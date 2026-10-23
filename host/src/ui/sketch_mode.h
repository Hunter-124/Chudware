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

#include <array>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "sketch_relations_panel.h"

struct AppState;
class Renderer;
struct Vec3;   // ../math.h — only used by-reference in signatures here (full def in the .cpp)

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
    // C52 sketch corner tools (engine ops sketch_fillet / sketch_chamfer; line-line corners):
    SkFillet, SkChamfer,
    // C53 sketch mirror / pattern / convert (engine ops sketch_mirror_entities /
    // sketch_pattern_entities / sketch_convert):
    Mirror, LinPattern, CircPattern, Convert,
    // C42: remaining draw tools (3-pt arc, polygon, center rect, point, ellipse, spline):
    Arc3pt, Polygon, RectCenter, Point, Ellipse, Spline,
    // C54: tangent arc (starts tangent to a prior/selected entity's free endpoint) + Move/Copy
    // entities (engine op sketch_move; select then pick a from-point and a to-point):
    ArcTangent, Move,
    // Sketch Text (entity type "text"): one viewport click places the lower-left anchor, then a
    // popup collects the string + cap height. Equation-Driven Curve (entity type "eq_curve"):
    // no viewport pick — a popup collects x(t)/y(t) expressions + a t range (origin-relative).
    Text, EqCurve,
    // Style Spline (entity type "style_spline"): click-collect CONTROL POLES (a control polygon);
    // the smooth curve is built by-poles in the engine. Same collect/finish UX as Spline.
    StyleSpline,
    // Airfoil calculator (no viewport pick; origin-relative like EqCurve): a popup collects a
    // NACA 4-/5-digit code or a .dat file + chord/angle-of-attack, then emits the upper/lower
    // surfaces as two through-point splines (chained into a closed, extrudable loop).
    Airfoil,
    // Polygon flyout: Star tool (2*polyStarPoints_ alternating outer/inner radii).
    Star,
    // Gizmo-driven bulk transforms (engine ops sketch_move / sketch_rotate). Select entities,
    // then drag an in-plane manipulator (arrows for move, a bent-arrow ring for rotate) with a
    // draggable centroid origin, live relation snapping, and a viewport HUD (offset / angle +
    // check/X to finish). Distinct from the simple from->to `Move` tool above.
    MoveEntities, RotateEntities
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
        return type == "distance" || type == "angle" || type == "radius" ||
               type == "diameter" || type == "length" || type == "arc_length";
    }
};

// One solved entity in the active sketch (mirrors a worker sketch_enter/solve entity record).
struct SketchEntity {
    std::string id;
    std::string type;            // line|circle|arc|slot|arc_slot|point|ellipse|spline
    std::string status;          // under|full|over (from the solver)
    bool construction = false;
    // Associative Convert Entities: owned by an external ref (distinct color);
    // stale = the source no longer resolves (dashed + warning color, last-good geometry).
    bool external = false;
    bool externalStale = false;
    bool externalPierce = false;
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
    void exitSketch(AppState& st, bool keep = true);   // Cancel deletes only a new empty sketch

    // Tree "Edit Sketch Plane": arm a plane pick that RE-PARENTS an existing sketch onto the picked
    // plane/face (rebuilds the sketch and every feature downstream). Exits the active sketch first if
    // it is the one being moved.
    void beginPlaneEdit(AppState& st, const std::string& sketchFid);
    // Consume a picked plane ref: re-parents the armed sketch when a plane edit is active, otherwise
    // creates+enters a new sketch (createSketchOnPlane). All plane-pick consumption sites (the
    // SketchMode overlay, the tree plane rows, the viewport plane chips) route through this.
    std::string consumePlanePick(AppState& st, const nlohmann::json& planeRef);

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

    // C51: feature-PM region picker. While a feature PropertyManager with a profile/sections well
    // is open, the candidate sketches' even-odd regions are fetched (doc_sketch_regions) and drawn
    // as fills in the viewport: subtle outline for every region, accent fill on hover, teal fill +
    // bold edge for regions currently in the profile. Hovering highlights; left-click toggles the
    // region into/out of the profile (whole-sketch <-> explicit, SolidWorks Selected-Contours). The
    // picker owns the mouse over a region (returns true) so model picking doesn't also fire. Called
    // from the Viewport panel when no sketch is being edited and a feature PM is active.
    bool drawRegionPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                          int vw, int vh, bool imgHovered);

    // Datum-plane picker for feature PropertyManager wells (reference axis "Two Planes", reference
    // plane "Mid Plane", …). While a feature PM is open with an active plane-accepting well
    // (PropertyManager::activeWellWantsPlane), the three origin datum planes are hit-tested with the
    // same frontmost-barycentric logic as the new-sketch plane pick: hovering highlights one, a
    // left-click routes its reserved datum ref ("plane.front"/…) into the well. Returns true while
    // the cursor sits over an origin plane so model picking doesn't also fire (the well still
    // accepts planar FACE picks through the normal id-buffer path when not over an origin plane).
    bool drawPlanePicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                         int vw, int vh, bool imgHovered);

    // Datum-axis picker for feature PropertyManager wells (revolve "Axis", circular-pattern axis).
    // While a feature PM is open with an active axis-accepting well (activeWellWantsAxis), the
    // displayed datum-axis lines (docMirror::refDatums kind=="axis") are hit-tested in screen space;
    // hovering highlights one and a left-click routes its datum ref into the well. Returns true while
    // the cursor is over a datum axis so model picking doesn't also fire (a click off any axis still
    // falls through to the id-buffer, so the well's model-edge pick still works).
    bool drawAxisPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                        int vw, int vh, bool imgHovered);

    // While a feature PM is open with an active well that accepts a point (selKinds.point),
    // the world-origin point glyph (and any point-datum features) are hit-tested in screen space.
    // Hovering highlights the glyph and a left-click routes "point.origin" into the well via
    // routeDatumPoint(). Returns true while the cursor is within pick radius so model picking
    // doesn't also fire.
    bool drawPointPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                         int vw, int vh, bool imgHovered);

    // While a feature PM is open with an active path/centerline well (activeWellWantsPathFeature),
    // the displayed curve datums (docMirror::refDatums kind=="curve" — a helix/spiral/spline) are
    // hit-tested in screen space; hovering highlights one and a left-click routes that curve feature
    // into the path well (routePathFeature). Returns true while the cursor is over a curve so model
    // picking doesn't also fire. Lets the user pick a 3D helix as a sweep path directly in the viewport.
    bool drawCurvePathPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                             int vw, int vh, bool imgHovered);

    // GENERAL datum selection (outside any PM picker): hover-highlight a displayed datum axis and,
    // on left-click, SELECT that datum feature (tree highlight + renderer emphasis). Returns true
    // while the cursor is over a datum axis so the model-pick click is suppressed. No-op while a PM
    // picker (axis/plane well) owns datum picks. Called from the viewport's normal selection path.
    bool drawDatumSelect(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                         int vw, int vh, bool imgHovered);

    // World-space centre of the active sketch's geometry (bbox centre of its entities, or the sketch
    // plane origin when empty), for the orbit-centre override so the view orbits/frames around the
    // sketch (not the body bbox / origin). Returns false when not editing a sketch.
    bool sketchOrbitCenter(Vec3& world) const;

    // True while a sketch entity/handle/group/dimension LMB drag is armed or live. The viewport
    // uses this to (a) freeze the sketch-bbox orbit-centre override for the drag's duration (the
    // bbox moves WITH the drag, so an unfrozen pivot re-translates the camera every frame — the
    // "view scrolls under my drag" bug) and (b) ignore MMB orbit/pan/zoom + wheel while dragging.
    bool entityDragActive() const;

    // GUI-picking overhaul: true when this frame's cursor is "hot" over a sketch entity, handle,
    // relation badge, region interior, or mid-interaction (drag/box-select armed or live), i.e.
    // sketch-entity picking claims the cursor. The viewport uses `!inSketchEntityHot()` to gate its
    // fallback model-geometry pick while a sketch is active — sketch entities keep priority, model
    // picking (faces/edges/vertices) only fires when the cursor is over neither. Valid only after
    // drawOverlay() has run this frame (defaults to true/safe before the first call).
    bool inSketchEntityHot() const { return entityHoverHot_; }

    // Refresh the model ID buffer for tool-owned external picks and explicit relation picking.
    bool needsModelPicks() const {
        return active_ && (tool_ == SketchTool::Convert || tool_ == SketchTool::SmartDim ||
                           relModelPicking_);
    }
    bool modelSelectionAllowed() const {
        return active_ && tool_ == SketchTool::None && relModelPicking_;
    }

    // Origin-plane index (0/1/2, -1 none) hovered by the PM plane picker last frame — fed to
    // Renderer::setPlanes as the emphasis so the hovered datum plane brightens (mirrors planeHover
    // for the new-sketch flow).
    int pmPlaneHover() const { return pmPlaneHover_; }

    // Short status line for the status bar ("Under Defined   DOF: 4"). Empty when inactive.
    std::string statusLine() const;

    // C40: ribbon cmd id of the armed tool ("" when none) — drives the ribbon's armed-tool
    // highlight (ui::setRibbonActiveCmd each frame from app.cpp).
    const char* activeToolCmd() const;

    // Clear in-sketch entity + relation selection WITHOUT changing the active tool (unlike
    // resetTool, which also exits the tool). Used by AppState::clearSelection for global deselect.
    void clearEntitySelection();
    // Replace the in-sketch entity selection with these handles ("e3", "e3.p1", "origin", ...);
    // unknown entity ids are dropped. Used by scripted scenarios to drive selection-based
    // commands (Add Relation, Fully Define on a subset) without synthetic viewport clicks.
    void selectEntities(const std::vector<std::string>& handles);

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

    // ---- Wave 4C/4D: sketch entity copy / paste ----
    // Driven by the global Ctrl+C / Ctrl+V routing (app.cpp) + the "edit_copy"/"edit_paste"
    // dispatcher ids while a sketch is active. Copy serializes the selected entities (+ their
    // internal constraints) into st.clipboard as an entities bundle, snapshotting a copy-origin
    // anchor (min-corner of the selection in sketch u,v). Paste enters a "paste placement" mode: the
    // pasted outlines follow the cursor; the next viewport click commits sketch_paste_entities with
    // offset = (click - copy-origin), keeping the pasted set's internal relations. Esc cancels the
    // placement. All are no-ops without an active sketch / selection / matching clipboard.
    bool sketchHasSelection() const;          // at least one entity selected (Ctrl+C is meaningful)
    bool sketchCopySelection(AppState& st);   // selection -> st.clipboard (entities bundle)
    bool sketchPasteBegin(AppState& st);      // arm paste-placement from a SketchEntities clipboard
    // Datum plane (0=front 1=top 2=right) clicked in the tree while Smart Dimension is armed:
    // project its intersection line with the sketch plane and add it as a dimension pick.
    void addPlaneDimRef(AppState& st, int planeIdx);

private:
    // ---- worker round-trips ----
    bool callSketch(AppState& st, const char* method, const nlohmann::json& extra,
                    nlohmann::json& result);
    // C50f: read-only over-define pre-flight (sketch_probe RPC). `action` is the exact action
    // object from worker_rpc.md §2 ({"add_constraint":{…}} / {"set_dim":{…}} / {"delete_constraint":{…}}).
    // On success fills `would_over_define` (+ conflicting/redundant of the probe) and returns true;
    // returns false on transport/RPC failure (caller falls back to committing normally, never blocks).
    bool sketchProbe(AppState& st, const nlohmann::json& action, bool& wouldOverDefine,
                     std::vector<std::string>& conflicting, std::vector<std::string>& redundant);
    void applyEnterResult(const nlohmann::json& res);
    void resolve(AppState& st);                 // sketch_solve -> refresh entities/statuses
    void commitDrag(AppState& st);              // sketch_drag_commit -> rebuild mesh
    void addEntity(AppState& st, const nlohmann::json& entity, const nlohmann::json& constraints);
    // Translate a stored snap handle (toolSnaps_ encoding) into the geometric constraint it
    // implies and append it to `cs`. `pt` is a "$self.<sub>" point handle on the new entity
    // (e.g. "$self.center"/"$self.start"). Plain handle -> coincident; "mid:" -> midpoint;
    // "rim:"/"tan:" -> point_on. Snaps onto an entity (no sub-handle) are no-ops. Shared by every
    // drawing tool so a snap captured at placement persists as a real relation (was Line-only).
    void pushSnapConstraint(const std::string& snap, const std::string& pt,
                            nlohmann::json& cs) const;
    // Lightweight single-entity / single-constraint emit used by the center-construction builders
    // (Polygon inscribed circle, Center Rectangle diagonal + center point). Always coalesces into
    // the in-progress undo step, so the whole shape stays ONE undo. Returns "" on failure.
    std::string emitConstructionEntity(AppState& st, const nlohmann::json& entity,
                                       const nlohmann::json& constraints);
    void emitConstructionConstraint(AppState& st, const nlohmann::json& constraint);
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
    // picks -> dimPending_ (+axis). keepAxis=true preserves the current dimAxis_ instead of
    // re-deriving it from the cursor zone (used when re-running after an arc-condition change in
    // the Modify box, where the dimension is already placed and its axis must not flip).
    bool dimBuildPending(const ImVec2& cursor, float cu, float cv, bool keepAxis = false);
    void dimOpenModify(const ImVec2& at);                       // open Modify box for dimPending_
    void dimCommit(AppState& st);                               // apply the Modify box value/expr
    // C50g: hit-test geometry for one dimension annotation, populated by drawDimension. SolidWorks
    // picks a dim by its TEXT box or its DIMENSION LINE (not the extension lines); we record both so
    // the hover loop can do a rect test + segment-distance test. Up to two segments (the diameter
    // leader straddles the circle as two collinear pieces; radius/distance use one).
    struct DimHit {
        std::string cid;
        ImVec2 textMin{0, 0}, textMax{0, 0};   // rendered label bounding rect (already padded)
        ImVec2 seg[2][2];                       // up to nSeg dimension-line segments (endpoints)
        int nSeg = 0;
    };
    // Render one dimension annotation (extension/dim lines, arrows, text). placement in (u,v).
    // hitOut (optional) receives the text rect + dim-line segments for SolidWorks-style picking.
    void drawDimension(ImDrawList* dl, const SketchConstraint& c, ImU32 lineCol, ImU32 textCol,
                       bool hovered, float pu, float pv, DimHit* hitOut = nullptr) const;
    // Compute a sensible default placement (u,v) for a constraint that has none yet.
    bool defaultPlacement(const SketchConstraint& c, float& pu, float& pv) const;
    const SketchEntity* entById(const std::string& id) const;
    bool relationPointUV(const std::string& handle, float& u, float& v) const;
    bool relationLineUV(const std::string& handle, ImVec2& a, ImVec2& b) const;

    // ---- projection (camera locked fronto-parallel to the plane) ----
    // Build the affine screen<->(u,v) basis for this frame from three projected plane points.
    void buildBasis(const Renderer& r, const ImVec2& imgMin, int vw, int vh);
    ImVec2 uvToScreen(float u, float v) const;
    void screenToUV(const ImVec2& s, float& u, float& v) const;

    // ---- tool helpers ----
    void resetTool();
    void cancelTool();
    // C49: per-frame alignment-inference result filled by snapPoint (dashed lines to the source
    // points). Set only when no point snap fired, or riding a "rim:" snap (rim point inline with
    // the origin / the curve's own center). Placement writes it as vertical/horizontal relations
    // when autoAlignRelations_ is on.
    struct SnapInfer {
        bool alignU = false, alignV = false;   // cursor u / v pulled to a source point's u / v
        ImVec2 srcU{0, 0}, srcV{0, 0};         // (u,v) of the alignment source point(s)
        std::string srcUHandle, srcVHandle;    // handle of each source point ("origin"/"e3.p1"/…)
    };
    // Snap a free (u,v) to nearby sketch geometry. *snapHandle encodes what fired:
    //   "e3.p2" / "e4.center" / "e5.p"  point snap -> coincident candidate (existing behavior)
    //   "mid:e3"                        line-midpoint snap -> midpoint-constraint candidate
    //   "rim:e5"                        circle/arc rim snap -> point_on (+ maybe tangent) candidate;
    //                                   with infer, also pulled along the rim to the spot inline
    //                                   with the origin or e5's center (infer->align*)
    //   ""                              nothing within range
    // Midpoint/rim/alignment fire only while inferenceOn_; plain point snap is always on.
    void snapPoint(float& u, float& v, std::string& snapHandle, SnapInfer* infer = nullptr,
                   const char* excludeHandle = nullptr,
                   const std::set<std::string>* excludeEnts = nullptr) const;
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

    // ---- C51: feature-PM region picker state (engine even-odd regions) ----
    struct PickRegion {
        int index = 0;                                  // stable region ordinal (matches engine)
        bool isMaterial = true;                         // even-depth = whole-sketch material
        float centroid[2] = {0, 0};
        std::vector<float> outer;                       // u,v,u,v,... boundary polyline
        std::vector<std::vector<float>> holes;          // each u,v,... (even-odd hit test)
        std::vector<float> tris;                        // u,v per vertex, 3 per tri (hole-aware fill)
    };
    struct RegionSketch {
        std::string fid;
        float frame[16] = {1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1};
        std::vector<PickRegion> regions;
        bool loaded = false;
    };
    void refreshRegionPicker(AppState& st);   // re-fetch candidate sketches on doc/rev/candidate change
    std::vector<RegionSketch> pickRegions_;
    std::string pickDocId_;
    int pickRevision_ = -999;
    std::string pickCandidatesKey_;   // joined candidate fids — refetch when the set changes
    int pickGen_ = 0;                 // supersede guard for in-flight region responses

    bool active_ = false;
    bool isNewEmpty_ = false;        // entered a freshly-created sketch (delete on cancel-exit)
    std::string fid_;
    std::string docId_;             // session owner, independent of the selected document
    int dof_ = 0;
    std::string status_;             // under|full|over
    std::vector<std::string> conflicting_;   // C50: constraint ids the solver flagged conflicting
    std::vector<std::string> redundant_;     // C50: constraint ids that remove no DOF (over-define)
    std::unordered_map<std::string, std::string> skipped_;

    // C50f: SolidWorks over-define pre-flight prompt for a NEW dimension. sketch_probe flagged the
    // pending add as over-defining BEFORE it was committed; this modal offers three choices —
    // Make Driven (default), Keep Driving, Cancel — and commits accordingly. The pending constraint
    // json (placement + value/expr already attached) is stashed in overDefPendingCon_ so the chosen
    // button commits it; nothing has been applied to the doc yet.
    bool overDefOpen_ = false;
    nlohmann::json overDefPendingCon_;   // the constraint to commit (Driven adds "driven":true)
    ImVec2 overDefPos_{0, 0};
    void drawOverDefinePrompt(AppState& st);
    void overDefCommit(AppState& st, bool driven);   // commit overDefPendingCon_ (driving|driven)

    // C50f: over-define pre-flight prompt for a dimension EDIT (sketch_set_dim). The probed new
    // value contradicts other relations; Cancel keeps the old value (sends nothing), Apply anyway
    // pushes it (the sketch goes over-defined with frozen geometry). The pending op is stashed.
    bool editOverDefOpen_ = false;
    nlohmann::json editOverDefOp_;       // the sketch_set_dim op to apply on "Apply anyway"
    ImVec2 editOverDefPos_{0, 0};
    void drawEditOverDefinePrompt(AppState& st);
    void applyDimEditOp(AppState& st, const nlohmann::json& op);   // shared set_dim apply + refresh

    // C50g: relation add pre-flight. Relations cannot be made driven, so an over-defining relation
    // gets a Cancel / Add anyway prompt before the op mutates the document.
    bool relOverDefOpen_ = false;
    nlohmann::json relOverDefOps_;
    std::string relOverDefLabel_;
    ImVec2 relOverDefPos_{0, 0};
    void addRelationOps(AppState& st, const nlohmann::json& ops, const std::string& label);
    void applyRelationOps(AppState& st, const nlohmann::json& ops, const std::string& label);
    void drawRelationOverDefinePrompt(AppState& st);

    // C50f: transient warning shown when a solve response carried solve_rejected:true (the
    // constraint system is contradictory and geometry is frozen at the last valid coords). Set by
    // applyEnterResult; cleared when a later solve comes back solve_rejected:false.
    bool solveRejected_ = false;
    float frame_[16] = {1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1};  // sketch(u,v,0)->world, row-major
    float camYaw_ = 0, camPitch_ = 89;
    // C34: pre-sketch camera, restored (animated) on exit.
    bool  havePrevCam_ = false;
    float prevYaw_ = 35, prevPitch_ = 30, prevZoom_ = 1;
    float prevPan_[3] = {0, 0, 0};

    std::vector<SketchEntity> ents_;
    std::unordered_map<std::string, size_t> entIndex_;
    size_t entitiesRevision_ = 0;
    nlohmann::json entitiesSnapshot_;
    bool regionsDirty_ = true;
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
    // Tree "Edit Sketch Plane": when set, the next plane pick RE-PARENTS this existing sketch
    // (edit_feature refs.plane) instead of creating a new sketch. Empty = normal new-sketch pick.
    // consumePlanePick() reads this; every pick consumption site routes through that helper.
    std::string planeEditFid_;
    // PM datum-plane picker hover (drawPlanePicker) — separate from the new-sketch planeHover_ so
    // the two flows don't cross-talk. -1 = none, else 0/1/2 = front/top/right.
    int  pmPlaneHover_ = -1;

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

    // When set, the vertical/horizontal ALIGNMENT inference (the dashed guide pulling a new point
    // onto another point's u or v) is also written as a real vertical/horizontal relation between
    // the two points — so an aligned placement stays aligned instead of being positional-only.
    // SolidWorks "automatic relations". Default ON; toggled from the in-sketch right-click menu.
    // Gated by inferenceOn_ (alignment only fires while inference is on).
    bool autoAlignRelations_ = true;

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

    // Emit a quad loop in one atomic batch with the given per-edge relations and placement snaps.
    // If center is set, include the center rectangle's construction point and diagonal in that
    // same batch. Returns the four line ids in corner order, or empty on failure.
    std::vector<std::string> addQuadLoop(AppState& st, const ImVec2 corners[4],
                     const char* relations[4], const std::string* cornerSnaps = nullptr,
                     const ImVec2* center = nullptr, const std::string& centerSnap = "");
    // Emit a closed N-line loop in one atomic batch. With center, include the inscribed
    // construction circle and placement snaps. Returns the line ids, or empty on failure.
    std::vector<std::string> addPolyLoop(AppState& st, const std::vector<ImVec2>& corners,
                                         bool equalEdges, const ImVec2* center = nullptr,
                                         const std::string& centerSnap = "",
                                         const std::string& vertexSnap = "");
    // C51: emit a straight slot as REAL geometry — 2 side lines + 2 cap arcs + 1 construction
    // centerline — joined by coincident endpoints (closure), the centerline ends coincident with
    // the arc centers, equal cap radii, and each side tangent to both caps. SolidWorks-style:
    // every piece is individually selectable + dimensionable (dimension to an arc center,
    // shift-pick two sides for a relation). The fresh slot stays under-defined (position/angle/
    // length/width free) until the user dimensions it. The tangent relations were once omitted
    // because our solver reports an endpoint-coincident tangent as redundant and the session then
    // flagged a false over-define; that is fixed (a redundant geometric relation no longer
    // over-defines — see engine/doc/session.py), so they are restored. p1/p2 = spine endpoints.
    // snap1/snap2 (optional, toolSnaps_ encoding) constrain the two spine endpoints — the cap-arc
    // centers — so a slot end dropped on a vertex/origin keeps its coincident/alignment.
    void addDecomposedSlot(AppState& st, const ImVec2& p1, const ImVec2& p2, float width,
                           const std::string& snap1 = "", const std::string& snap2 = "");
    // C42: Polygon tool side count (set with number keys 3-9 while the tool is armed).
    int polySides_ = 6;
    // Centerline: reuses the Line tool's click-collect/inference/snap path, but commits the segment
    // as a construction line plus an exaggerated construction midpoint marker (there is no distinct
    // engine "centerline" sketch entity). Set while arming Centerline; captured/restored across the
    // Line commit's chain re-arm (mirrors polySides_) so continued segments stay centerlines.
    bool centerline_ = false;
    // Star tool parameters (set while tool_ == SketchTool::Star).
    int   polyStarPoints_ = 5;    // number of tips (clamped 3..12)
    float polyStarRatio_  = 0.45f; // inner/outer radius ratio

    // Add Relation snapshots sketch handles, then optionally collects model selections.
    bool relPopupOpen_ = false;
    ImVec2 relPopupPos_{0, 0};
    std::vector<std::string> relIds_;
    bool relModelPicking_ = false;
    struct RelationModelRef {
        nlohmann::json source;
        std::vector<SketchEntity> entities;
        std::vector<std::string> labels;
        int selectedEntity = 0;
        bool canPierce = false;
    };
    std::vector<RelationModelRef> relModelRefs_;
    std::string relModelKey_;
    void beginRelations(AppState& st);
    void refreshRelationModelRefs(AppState& st);
    void drawAddRelations(AppState& st, bool& escHandled);
    void fullyDefine(AppState& st);
    RelationsPanelState relationsPanel_;
    std::string panelHoverRel_;
    void drawRelationList(AppState& st, const ImVec2& lo, const ImVec2& hi);

    // drag state
    std::string dragHandle_;
    bool dragging_ = false;
    int dragFrame_ = -1;             // last frame a sketch_drag sample was *queued* (UI throttle)
    // Single-in-flight latest-wins sketch_drag. Each mouse sample overwrites
    // dragPendingExtra_; when no RPC is open, kickSketchDragAsync submits it. Completions with
    // gen != dragGen_ are dropped; a pending overwrite re-kicks after drain.
    int  dragGen_ = 0;
    bool dragInFlight_ = false;
    bool dragPending_ = false;
    nlohmann::json dragPendingExtra_;
    void queueSketchDrag(AppState& st, nlohmann::json extra);
    void kickSketchDragAsync(AppState& st);
    // C50: click-vs-drag on a point handle. Mouse-down near an endpoint/center ARMS a potential
    // drag; moving past kDragThreshPx starts the drag, releasing without moving SELECTS that point
    // handle (so vertices can be picked for coincident / alignment relations, SolidWorks-style).
    bool dragArmed_ = false;
    std::string pendingHandle_;
    ImVec2 dragDownPos_{0, 0};

    // C56: drag snap-and-relate (Settings::sketchDragSnapRelations, default on). While a point
    // handle or a whole-entity/group translate is being dragged, the dragged point snaps to the
    // origin / endpoints / centers / midpoints / rims and u/v alignment guides (line endpoint
    // drags additionally infer horizontal/vertical/parallel/collinear direction). The pending
    // relation is previewed as cursor chips + the usual snap markers, and committed as real
    // sketch_add_constraint ops on drop (applyDragSnapRelations).
    bool        dragSnapLive_ = false;   // preview state valid this frame (a drag is active)
    std::string dragSnapHandle_;         // snapPoint-encoded target ("origin"/"e3.p1"/"mid:e3"/"rim:e4")
    SnapInfer   dragSnapInfer_;          // u/v alignment guides fired during the drag
    std::string dragRelType_;            // inferred line relation: horizontal|vertical|parallel|collinear
    std::string dragRelEnt_;             // dragged line the relation belongs to
    std::string dragRelOther_;           // other entity id for parallel/collinear
    std::string groupSnapSrc_;           // group drag: dragged keypoint handle that fired the snap
    std::string dragAlignSrcU_, dragAlignSrcV_;  // group drags: dragged keypoint per alignment axis
    ImVec2      dragSnapUV_{0, 0};       // snapped dragged-point position (marker/chip anchor)
    // Snap a live group translate: adjusts (gu,gv) so the fired keypoint/alignment/collinear
    // snap lands exactly and fills the drag-snap preview state above.
    void groupDragSnap(float& gu, float& gv);
    // Snap a circle/arc radius (.rim handle) drag: adjusts r so the rim passes through a point,
    // touches a line or another circle/arc (tangent), or matches another radius (equal).
    void rimDragSnap(const SketchEntity& e, float& r);
    // Emit the previewed relations as constraints after sketch_drag_commit. `srcHandle` is the
    // dragged point (dragHandle_ or groupSnapSrc_); skips relations that already exist.
    void applyDragSnapRelations(AppState& st, bool groupDrag);
    bool constraintExists(const std::string& type, const std::vector<std::string>& refs) const;

    // C41 (T2): in-sketch selection (tool_ == None). Left-click on/near an entity selects it
    // (Ctrl+click toggles multi-select); Delete/Backspace deletes the selection via
    // sketch_delete_entity ops. Cleared by resetTool (and thus every exit/cancel path) and
    // pruned of vanished ids on every applyEnterResult refill.
    std::vector<std::string> selIds_;
    bool isSelected(const std::string& id) const;

    // ---- Wave 4C/4D: paste-placement state ----
    // pastePlacing_ is true between sketchPasteBegin() and the placement click (or Esc). The pasted
    // outlines are previewed at (cursor - pasteAnchorUV_) each frame; the commit click sends
    // sketch_paste_entities with offset = cursorUV - pasteAnchorUV_. The bundle to paste + its anchor
    // are snapshotted off st.clipboard at begin so a later clipboard change can't disturb the preview.
    bool pastePlacing_ = false;
    nlohmann::json pasteBundle_;            // {entities, constraints} being placed
    ImVec2 pasteAnchorUV_{0, 0};            // copy-origin in sketch (u,v) (preview/offset base)
    void commitPasteEntities(AppState& st, float offsetX, float offsetY);
    // Draw the follow-cursor preview of the paste bundle (outlines shifted by cursor - anchor).
    void drawPastePreview(AppState& st, ImDrawList* dl, const ImVec2& cursor);

    // ---- C51: box (rubber-band) selection ----
    // Dragging from empty canvas rubber-bands a selection rectangle (SolidWorks style):
    // left->right = WINDOW (only entities fully enclosed); right->left = CROSSING (any entity
    // the box touches). Ctrl makes the box ADD to the current selection instead of replacing it.
    // A press in empty space arms the box (boxArmed_); moving past the threshold promotes it to a
    // live drag (boxSelecting_); release that never moved is a plain click (clears, or selects a
    // hot profile region — see below).
    bool boxArmed_ = false;
    bool boxSelecting_ = false;
    bool boxCtrl_ = false;            // Ctrl state captured at press (additive box select)
    ImVec2 boxStart_{0, 0};           // anchor corner (screen) of the rubber band
    // Apply a finished box: select every entity window-enclosed (crossing=false) or touched
    // (crossing=true) by the screen rect (a,b). additive keeps the existing selection.
    void boxSelect(const ImVec2& a, const ImVec2& b, bool crossing, bool additive);

    // ---- C51: closed-profile (region) shading + one-click loop select ----
    // Closed loops are detected client-side from the live, non-construction entities (mirrors the
    // engine connectivity walk in features/sketch.py). Each region is shaded faintly. Clicking a
    // region's interior selects its whole loop IMMEDIATELY (no dwell). The dwell is repurposed:
    // resting the cursor on a single entity edge for kLoopDwell seconds previews (highlights) the
    // loop that edge belongs to, so a click then grabs the whole loop instead of the lone edge.
    struct SketchRegion {
        std::vector<std::string> ids;   // entity ids forming the closed loop
        std::vector<ImVec2> poly;       // boundary polygon in sketch (u,v), CCW-ish, not closed-dup
        float area = 0.0f;              // |signed area| (or pi r^2 for circles) for nesting
        // Even-odd nesting (mirrors core/geom/profile.cpp nest_loops): parent = smallest-area
        // enclosing loop, depth = # of enclosers, isMaterial = (depth % 2 == 0). Direct child
        // holes are punched when shading material regions so concentric loops read as washers.
        int parent = -1;
        int depth = 0;
        bool isMaterial = true;
        std::vector<int> holeChildren;  // direct child region indices (voids to punch)
        bool isCircle = false;          // true for single-entity full circle (radius containment)
        float circleCenter[2] = {0, 0};
        float circleRadius = 0.0f;
        std::vector<ImVec2> triangles;   // UV triangulation, rebuilt only when geometry changes
    };
    std::vector<SketchRegion> regions_;
    void rebuildRegions();                            // refill regions_ from ents_
    int  hitTestRegion(const ImVec2& s) const;        // smallest region under screen pt, or -1
    int  regionContaining(const std::string& entId) const;   // multi-entity loop holding entId, or -1
    // Sample an entity's boundary into sketch-(u,v) points. closed=true for self-closed shapes
    // (circle/ellipse/slot); for line/arc the two endpoints bound an open polyline. Used by region
    // detection, box-select containment, hit-testing and hover outlines.
    void entityBoundaryUV(const SketchEntity& e, std::vector<ImVec2>& out, bool& closed) const;
    // Stroke an entity's outline (hover pre-highlight / loop preview), drawn on top of geometry.
    void strokeEntityOutline(ImDrawList* dl, const SketchEntity& e, ImU32 c, float th) const;
    bool regionValid(int idx) const { return idx >= 0 && idx < (int)regions_.size(); }
    int   hotRegion_ = -1;            // region to render brightened this frame (hover/loop preview)
    // Entity-edge dwell -> loop preview. While the cursor rests on the same single entity that
    // belongs to a closed loop, dwellAccum_ grows; past kLoopDwell, entityLoopRegion_ names that
    // loop so the pre-highlight + click target expand from the lone edge to the whole loop.
    std::string dwellEnt_;
    float dwellAccum_ = 0.0f;
    int   entityLoopRegion_ = -1;

    // ---- C51: geometric-relation selection ----
    // Relation badges (coincident / h / v / parallel / …) are click-selectable like entities. A
    // selected relation renders in the accent color and Delete removes it (sketch_delete_constraint).
    // relHits_ is refilled every frame as the badges are drawn (screen rects + constraint id), then
    // hit-tested by the interaction pass; hoverRelCid_ carries one frame of hover for the tint.
    struct RelHit { std::string cid; ImVec2 lo, hi; };
    std::vector<RelHit> relHits_;
    std::string hoverRelCid_;
    struct RelationBadge {
        std::string cid, type;
        ImVec2 pos, anchor;
    };
    std::vector<RelationBadge> relationBadges_;
    size_t badgesRevision_ = size_t(-1);
    std::array<float, 7> badgesCamera_{};
    // GUI-picking overhaul: set each frame by drawOverlay's hover pass; see inSketchEntityHot().
    // Defaults true so a stale/pre-first-frame read never opens the model-pick fallback by mistake.
    bool entityHoverHot_ = true;
    std::vector<std::string> selRelIds_;
    bool isRelSelected(const std::string& cid) const;
    int  hitTestRelationBadge(const ImVec2& s) const;   // index into relHits_, or -1

    // ---- C51: whole-entity / profile / multi-select DRAG (translate, not reshape) ----
    // Pressing on an entity BODY (not a vertex handle) or inside a multi-entity profile arms a
    // group drag; moving past the threshold translates every entity in the set together via the
    // sketch_drag `targets` list (each defining point soft-targeted at orig+delta, so constraints
    // stay exact). A press that never moves falls back to a plain click (select entity / loop).
    bool groupDragArmed_ = false;
    bool groupDragging_ = false;
    ImVec2 groupDragStartUV_{0, 0};
    std::vector<std::pair<std::string, ImVec2>> groupDragTargets_;   // (handle, orig u,v)
    int groupClickKind_ = 0;          // release-without-move: 0=clear/none, 1=select entity, 2=loop
    std::string groupClickEnt_;       // entity id for kind 1
    int groupClickRegion_ = -1;       // region index for kind 2
    // Enumerate the point handles that rigidly translate entity e, with their current (u,v).
    void entityDragHandles(const SketchEntity& e,
                           std::vector<std::pair<std::string, ImVec2>>& out) const;
    // Begin a group drag of `ids` (capture their translation handles from the current coords).
    void beginGroupDrag(const std::vector<std::string>& ids, float startU, float startV);
    // Push one frame of the in-progress group translation to the worker (delta from start).
    void updateGroupDrag(AppState& st, float curU, float curV);

    // Trim power-sweep state: LMB held after a trim keeps trimming entities the cursor crosses.
    bool trimSweeping_ = false;
    ImVec2 trimSweepLast_{0, 0};
    // Trim option: keep the trimmed-away span as construction geometry (still constrained to
    // the surrounding sketch) instead of deleting it. Toggled in the Trim tool HUD.
    bool trimKeepConstruction_ = false;

    // C29-B Offset tool state. Stages: collect a chain of entity ids (selected ones tint accent),
    // then a distance overlay (InputFloat near cursor, Enter commits), then one click for the side
    // hint -> sketch_offset op. offsetStage_: 0=picking chain, 1=typing distance, 2=picking side.
    std::vector<std::string> offsetChain_;
    int offsetStage_ = 0;
    float offsetDist_ = 10.0f;
    ImVec2 offsetOverlayPos_{0, 0};
    bool offsetFocusInput_ = false;   // request keyboard focus on the distance field next frame

    // ---- C52 Sketch Fillet / Chamfer state (line-line corners; engine sketch_fillet/chamfer) ----
    // Both tools pick two lines in turn. corner1Id_ holds the first picked line ("" = none yet);
    // the second pick builds the op and applies it. radius/distance persist across corners and
    // across invocations within the session (SolidWorks remembers the last value). They stay armed
    // for another corner after each success; Esc clears the first pick (or exits if none held).
    std::string corner1Id_;           // first picked line id ("" = waiting for line 1)
    float skFilletRadius_ = 3.0f;     // mm, persisted
    float skChamferDist_ = 3.0f;      // mm, persisted
    // Apply one sketch_fillet/chamfer op (e1=corner1Id_, e2=line2) and refresh. On engine OpError
    // the message is toasted, the sketch is untouched, and corner1Id_ is reset (pick a fresh pair).
    void applyCornerOp(AppState& st, const std::string& line2);

    // ---- C53 Mirror / Linear Pattern / Circular Pattern / Convert state ----
    // Mirror + both patterns share an entity-selection stage (mpSel_, click to toggle, like the
    // Offset chain). Mirror then picks a construction line; Linear/Circular collect numeric dims in
    // an inline panel (count + spacing/angle) — Circular also picks a center point. Convert picks
    // MODEL edges/faces in the viewport and projects each via sketch_convert (no selection stage).
    std::vector<std::string> mpSel_;   // entities selected for mirror/pattern (in pick order)
    // Stage machine (per active tool):
    //   Mirror:  0=pick entities (Enter advances), 1=pick the mirror construction line.
    //   LinPattern:  0=pick entities (Enter advances), 1=set count/spacing/dir in the panel.
    //   CircPattern: 0=pick entities (Enter advances), 1=pick the center point, 2=panel.
    int mpStage_ = 0;
    int patternCount_ = 4;             // shared instance count (persisted)
    float patternSpacing_ = 25.0f;     // linear: spacing (mm)
    float patternAngle_ = 60.0f;       // circular: angle (deg) — total span when equalSpacing_
    bool patternEqualSpacing_ = true;  // circular: angle is the full span shared equally
    float patternDirU_ = 1.0f, patternDirV_ = 0.0f;   // linear direction (sketch u,v)
    float patternCenterU_ = 0.0f, patternCenterV_ = 0.0f;  // circular center (sketch u,v)
    // Apply the built sketch_mirror_entities / sketch_pattern_entities op + refresh (toast on error).
    void applyMirrorOp(AppState& st, const std::string& lineId);
    void applyPatternOp(AppState& st, bool circular);

    // ---- C54 Move/Copy entities (engine op sketch_move) ----
    // Reuses the mirror/pattern selection stage (mpSel_) for stage 0: click line/circle/arc entities
    // to toggle them in the move set, Enter advances. Stage 1: click a FROM point (anchor), stage 2
    // is the live drag to a TO point; the second click applies sketch_move {entities, dx, dy, copy}
    // with delta = to - from. Holding Ctrl at the apply click sets copy=true (SolidWorks: Ctrl-drag
    // copies). The from-point snaps to a vertex/center if near one. Esc backs out a stage.
    ImVec2 moveFromUV_{0, 0};          // captured anchor (u,v) when stage 1 picks the from-point
    bool moveCopy_ = false;            // copy mode (Ctrl held at the apply click), persisted as hint
    // Apply the built sketch_move op (delta + copy flag) over mpSel_ + refresh (toast on error).
    void applyMoveOp(AppState& st, float dx, float dy, bool copy);
    // One conversion operation for all resolved refs (one compile/reload/undo).
    void applyConvertRefs(AppState& st, const nlohmann::json& refs);

    // ---- Gizmo-driven Move/Rotate Entities (engine ops sketch_move / sketch_rotate) ----
    // Stage 0 reuses mpSel_ (click line/circle/arc to toggle). Advancing (Enter or first drag)
    // seeds gizmoOrigin_ at the selection centroid. Stage 1 shows an in-plane manipulator at
    // gizmoOrigin_: Move draws two axis arrows (drag = translate; center square drag repositions
    // the origin); Rotate draws a bent-arrow ring (drag = spin about the plane normal). A green
    // check / red X (Enter / Esc, or the viewport HUD) commits / cancels. Live values feed the HUD.
    ImVec2 gizmoOrigin_{0, 0};          // manipulator origin in (u,v) = move basis / rotation axis
    bool gizmoOriginInit_ = false;      // false until stage 0 -> 1 seeds the centroid
    bool gzDragging_ = false;           // a handle is being dragged this gesture
    int gzDragAxis_ = -1;               // -1 none, 0=u arrow, 1=v arrow, 2=free/center, 3=rotate ring
    ImVec2 gzDragStartUV_{0, 0};        // (u,v) under the cursor when the drag began
    ImVec2 gzOriginAtDragStart_{0, 0};  // gizmoOrigin_ snapshot for center-repositioning
    float moveDx_ = 0.0f, moveDy_ = 0.0f;   // live translation offset (mm), shown in HUD
    float rotStartAng_ = 0.0f;          // pointer angle (rad) about origin when a rotate drag began
    float rotCurAng_ = 0.0f;            // current net rotation (degrees), shown/edited in HUD
    bool rotDidType_ = false;           // HUD angle field committed a typed value this frame
    // Compute the (u,v) centroid of the mpSel_ bounding box; false if selection empty.
    bool computeSelCentroid(ImVec2& out) const;
    // Apply sketch_rotate {entities, cx, cy, angle, copy} over mpSel_ + refresh (toast on error);
    // diffs horizontal/vertical relations before/after to report swaps/drops.
    void applyRotateOp(AppState& st, float cx, float cy, float angleDeg, bool copy);

public:
    // Viewport HUD (drawn by viewport_panel below the file title): state of an active gizmo
    // transform. kind 0 = none, 1 = MoveEntities, 2 = RotateEntities.
    struct TransformHud { int kind = 0; float dx = 0, dy = 0; float angle = 0; };
    TransformHud transformHudState() const;
    void setHudAngle(float deg) { rotCurAng_ = deg; rotDidType_ = true; }
    void commitTransform(AppState& st);   // finish (green check) — applies the pending gizmo op
    void cancelTransform();               // cancel (red X) — drops the gesture, back to select
private:
    bool hudFinish_ = false, hudCancel_ = false;   // set by HUD buttons, consumed by the tool branch

    // ---- C54 Tangent Arc (draw an arc that starts tangent to a prior/selected entity) ----
    // The start point is the FREE endpoint of a source entity (the most-recently-drawn entity, or
    // the last selected one if a selection is held); the arc's start tangent direction equals the
    // source entity's direction at that endpoint. The user then clicks the arc's end; we solve the
    // unique tangent arc through (start, end) with that start tangent, and commit it with a
    // `tangent` relation (arc<->source) + a `coincident` joining the shared endpoint so it stays
    // tangent under drag. Source types: line (tangent along the segment) and arc/circle (tangent
    // perpendicular to the radius). Re-resolved each frame while armed (snapped start highlighted).
    // Returns false (no valid source) -> the tool falls back to a plain 3-pt-style arc.
    struct TangentArcSrc {
        std::string id;                 // source entity id ("" = none found)
        std::string handle;             // shared endpoint handle on the source ("eN.p2"/"eN.start"/…)
        ImVec2 start{0, 0};             // the free endpoint (u,v) = the arc start
        ImVec2 tdir{1, 0};              // unit start tangent direction (u,v), pointing into the arc
    };
    // Resolve the tangent-arc source: the source entity nearest the cursor's pick, its endpoint
    // nearest the cursor, and the outward tangent there. False when no line/arc/circle is available.
    bool tangentArcSource(const ImVec2& cursorScreen, TangentArcSrc& out) const;
    // Solve the arc tangent to `tdir` at `start` and passing through `end`: fills center/radius/
    // start-angle/end-angle(deg)/ccw. Returns false when start==end or the geometry is degenerate
    // (end lies on the tangent line through start -> infinite radius).
    static bool tangentArcThrough(const ImVec2& start, const ImVec2& tdir, const ImVec2& end,
                                  float& cu, float& cv, float& rad,
                                  float& a0deg, float& a1deg, bool& ccw);

    // ---- Sketch Text (entity type "text"; engine geometry already implemented) ----
    // The SketchTool::Text arms a one-click placement: the next viewport left-click captures the
    // lower-left anchor into textAnchorUV_ and opens this popup, which collects the string + cap
    // height. OK emits {"type":"text","p":[u,v],"text":...,"height":...,"construction":false}
    // (font omitted -> engine default). Esc/Cancel closes; empty text disables OK.
    bool  textPopupOpen_ = false;
    ImVec2 textPopupPos_{0, 0};
    ImVec2 textAnchorUV_{0, 0};        // captured lower-left placement (sketch u,v)
    char   textBuf_[256] = "OPEN";
    float  textHeight_ = 10.0f;        // cap height mm (>0)
    bool   textFocus_ = false;         // request keyboard focus on the string field next frame
    void   drawTextPopup(AppState& st, bool& escHandled);

    // ---- Equation-Driven Curve (entity type "eq_curve"; engine geometry already implemented) ----
    // SketchTool::EqCurve opens this popup immediately (no viewport pick; the curve is origin-
    // relative like SolidWorks). x(t)/y(t) are expression strings in t (radians); t1/t2 bound the
    // parameter range. OK emits {"type":"eq_curve","xt":...,"yt":...,"t1":...,"t2":...,
    // "construction":false}. A closed curve (e.g. cos/sin over 0..2pi) extrudes as a region.
    bool  eqPopupOpen_ = false;
    ImVec2 eqPopupPos_{0, 0};
    char   eqXt_[128] = "5*cos(t)";
    char   eqYt_[128] = "5*sin(t)";
    float  eqT1_ = 0.0f;
    float  eqT2_ = 6.2831853f;
    bool   eqFocus_ = false;           // request keyboard focus on the x(t) field next frame
    void   drawEqCurvePopup(AppState& st, bool& escHandled);

    // ---- Airfoil calculator (SketchTool::Airfoil; entities = two through-point splines) ----
    // The ribbon command opens this popup immediately (origin-relative, like Eq-Driven Curve).
    // It generates a NACA 4-/5-digit section or loads a Selig/Lednicer .dat file, scaled to
    // `chord` and rotated by the angle of attack. OK emits the upper+lower surfaces as splines
    // (plus a trailing-edge line when the TE is open) chained into one closed loop via addAirfoil.
    bool   afPopupOpen_ = false;
    ImVec2 afPopupPos_{0, 0};
    int    afSeries_ = 0;              // 0 = NACA 4-digit, 1 = NACA 5-digit, 2 = .dat file
    char   afCode_[16] = "2412";      // NACA code (4 or 5 digits depending on series)
    float  afChord_ = 100.0f;         // chord length (mm)
    float  afAoA_ = 0.0f;             // angle of attack (deg; +nose up)
    int    afPoints_ = 80;            // sample points per surface (cosine-spaced)
    bool   afClosedTE_ = true;        // closed (sharp) vs open trailing edge
    std::string afDatPath_;           // chosen .dat file (series 2)
    bool   afFocus_ = false;          // request keyboard focus on the code field next frame
    void   drawAirfoilPopup(AppState& st, bool& escHandled);
    // Emit a generated profile (upper/lower surface point arrays, mm) as chained splines.
    void   addAirfoil(AppState& st, const std::vector<std::array<double, 2>>& upper,
                      const std::vector<std::array<double, 2>>& lower, bool sharpTE);

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
