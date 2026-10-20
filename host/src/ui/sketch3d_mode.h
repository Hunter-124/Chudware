// 3D Sketch mode UI — the free-space sketcher environment for `sketch3d` features
// (contracts/sketch3d_gui_contract.md). Deliberately LEANER than the 2D SketchMode: same
// structural conventions (worker RPC via WorkerClient::call, entities drawn as an ImGui
// draw-list overlay projected through Renderer::CameraXform, ribbon/tree entry points),
// but no plane lock — the camera stays free (MMB orbit) and points are placed on a movable
// WORKING PLANE with snapping to existing sketch points / the origin / axis directions.
//
// Surface: point / line / 3-pt arc / spline entities; 3D geometric relations, dimensions,
// datum/working-plane relations and associative model-vertex reference points.
// All edits are plain doc ops (add_feature / edit_feature params patches); the two read-only
// RPCs are sketch3d_health (solve snapshot) and sketch3d_drag (transient constrained drag,
// committed on mouse-up as one edit_feature = one undo step).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "../vec_math.h"

struct AppState;

namespace ui {

// The active 3D-sketch drawing tool. None = selection/drag mode.
enum class Sk3Tool { None, Line, Arc3, Spline, Point };

// One solved entity, uniform point-list form (point:1, line:2, arc:3 [p1,pm,p2], spline:N).
struct Sk3Entity {
    std::string id;
    std::string type;            // point|line|arc|spline
    std::string status = "under";
    bool construction = false;
    std::vector<Vec3> pts;       // world mm, solved coords
    std::vector<Vec3> samples;   // engine-sampled interpolating spline, shared with solver geometry
};

class Sketch3DMode {
public:
    bool active() const { return active_; }
    const std::string& sketchId() const { return fid_; }

    // Enter an existing sketch3d feature (tree double-click / "Edit 3D Sketch").
    bool enterForFeature(AppState& st, const std::string& fid);
    // Create a fresh empty sketch3d feature and enter it (ribbon "3D Sketch").
    bool enterNew(AppState& st);
    // keep=true commits (all edits are already applied doc ops — just leave the mode);
    // keep=false reverts to the entry checkpoint (and deletes a still-empty new sketch).
    void exitSketch(AppState& st, bool keep = true);

    // Ribbon dispatch ("sketch3d_new" from anywhere; everything else in-overlay). True = handled.
    bool onRibbonCommand(AppState& st, const std::string& cmdId);

    // Draw the in-viewport overlay (working-plane grid, entities, handles, tool rubber-band,
    // toolbar, status) and process mouse/keyboard. Called from the Viewport panel after the
    // 3D image, with the image rect + size and hover state.
    void drawOverlay(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                     int vw, int vh, bool imgHovered);

    // True while an LMB entity/handle drag is armed or live — the viewport freezes ALL camera
    // motion (MMB orbit/pan/zoom + wheel) for the drag's duration (dead-zone rule; only the
    // overlay's own edge auto-pan may move the view).
    bool entityDragActive() const { return dragArmed_ || dragging_; }

    // Status-bar line ("Fully Defined" / "Under Defined   DOF: 4" / "Over Defined").
    std::string statusLine() const;

    // GUI-picking overhaul: true when this frame's cursor is "hot" over a 3D-sketch entity/handle
    // or a drawing tool/drag owns it. The viewport gates its fallback model-geometry pick on
    // `!inSketchEntityHot()` — sketch entities keep priority, model picking is the fallback. Set
    // each frame by drawOverlay(); defaults true (safe) before the first call.
    bool inSketchEntityHot() const { return entityHoverHot_; }

private:
    // ---- worker round-trips ----
    bool callSk3(AppState& st, const char* method, const nlohmann::json& extra,
                 nlohmann::json& result);
    void refreshHealth(AppState& st);                 // sketch3d_health -> ents_/dof_/status_
    void applyEntities(const nlohmann::json& res);    // parse entities[] + statuses{}
    // Persist a params patch (full entities and/or constraints arrays) via edit_feature.
    bool commitParams(AppState& st, const nlohmann::json* entities,
                      const nlohmann::json* constraints, const char* label);
    // Append one entity (+ extra constraints) to the persisted params; returns new entity id.
    std::string addEntity(AppState& st, nlohmann::json entity, nlohmann::json constraints);
    void addConstraint(AppState& st, nlohmann::json con, const char* label);
    std::string nextId(const char* prefix) const;     // "e<N>"/"c<N>" unique in cached params
    void setModelVertexPicking(AppState& st, bool enabled);
    void consumeModelVertexPick(AppState& st);
    bool isExternalHandle(const std::string& handle) const;
    bool modelVertexPicking_ = false;
    bool savedPickFaces_ = true, savedPickEdges_ = true, savedPickVertices_ = true;

    // ---- working plane ----
    // Presets 0=XY(z-up top) 1=XZ 2=YZ 3=view-aligned. Anchored at wpO_ (origin, or the last
    // placed/snapped point so a polyline chain can turn in space). Tab cycles presets.
    void setPlanePreset(AppState& st, int preset);
    void drawPlaneGrid(ImDrawList* dl, AppState& st, const ImVec2& imgMin, int vw, int vh) const;
    int wpPreset_ = 0;
    Vec3 wpO_{0, 0, 0}, wpU_{1, 0, 0}, wpV_{0, 1, 0};

    // ---- picking / snapping ----
    // Cursor -> world point on the working plane, with snapping: existing sketch point handles
    // + the origin (within kSnapPx, exact 3D point; handle recorded) and axis inference from
    // `prev` (candidate pulled onto the X/Y/Z line through prev; axis recorded as "x"/"y"/"z").
    bool cursorWorld(AppState& st, const ImVec2& mouse, const ImVec2& imgMin, int vw, int vh,
                     const Vec3* prev, Vec3& out, std::string& snapHandle,
                     std::string& axisSnap) const;
    // Nearest point handle under the screen point ("e3.p1"/"e2.p"/"origin"); dist in px.
    bool hitTestHandle(AppState& st, const ImVec2& s, const ImVec2& imgMin, int vw, int vh,
                       std::string& handleOut, Vec3& worldOut) const;
    // Nearest whole entity polyline under the screen point; returns id or "".
    std::string hitTestEntity(AppState& st, const ImVec2& s, const ImVec2& imgMin,
                              int vw, int vh) const;
    // Sample an entity to a world polyline (arc circumcircle / engine-sampled spline).
    static void samplePolyline(const Sk3Entity& e, std::vector<Vec3>& out);
    const Sk3Entity* entById(const std::string& id) const;
    Vec3 handleWorld(const std::string& handle) const;   // solved coords of a point handle

    // ---- tools ----
    void resetTool();
    void commitLine(AppState& st);
    void commitArc(AppState& st);
    void commitSpline(AppState& st);
    void commitPoint(AppState& st);

    // ---- selection & constraints ----
    void toggleSel(const std::string& idOrHandle);
    void clearSel() { selEnts_.clear(); selHandles_.clear(); }
    // Contextual constraint/dimension buttons for the current selection (in the toolbar).
    void drawConstraintButtons(AppState& st);
    void deleteSelection(AppState& st);
    // Dimension value popup (distance/length/angle).
    bool dimPopupOpen_ = false;
    nlohmann::json dimPendingCon_;      // constraint awaiting a value
    double dimValue_ = 0.0;
    bool dimFocus_ = false;
    void drawDimPopup(AppState& st);
    void drawPlanePopup(AppState& st);
    bool planePopupOpen_ = false;
    nlohmann::json planePendingCon_;

    // ---- drag ----
    void updateDrag(AppState& st, const ImVec2& mouse, const ImVec2& imgMin, int vw, int vh);
    void commitDrag(AppState& st);

    bool active_ = false;
    bool isNewEmpty_ = false;
    std::string fid_;
    std::string docId_;                 // guard: auto-exit if the active doc changes
    std::string cancelCheckpoint_;

    // cached persisted params (source of truth for edits; refreshed from the doc tree)
    nlohmann::json entJson_ = nlohmann::json::array();
    nlohmann::json conJson_ = nlohmann::json::array();
    nlohmann::json externalRefs_ = nlohmann::json::array();
    void reloadParamsFromTree(AppState& st);

    std::vector<Sk3Entity> ents_;       // solved display coords (health / last drag response)
    int dof_ = 0;
    std::string status_ = "under";
    bool converged_ = true;
    int nWires_ = 0;
    std::string warning_;

    Sk3Tool tool_ = Sk3Tool::None;
    std::vector<Vec3> toolPts_;
    std::vector<std::string> toolSnaps_;   // per-click point-handle snap ("" = none)
    std::vector<std::string> toolAxes_;    // per-SEGMENT axis inference ("x"/"y"/"z"/"")
    std::string chainPrevHandle_;          // line chaining: previous segment's p2 handle
    bool constructionOn_ = false;

    std::vector<std::string> selEnts_;
    std::vector<std::string> selHandles_;

    // drag state (mirrors the 2D sketcher's arm->promote pattern; kDragArmPx = 5*uiScale)
    bool dragArmed_ = false;
    bool dragging_ = false;
    std::string dragHandle_;
    Vec3 dragOrig_{0, 0, 0};            // handle world pos at press (drag plane anchor)
    ImVec2 dragDownPos_{0, 0};
    int dragFrame_ = -1;
    nlohmann::json dragEntities_;       // last sketch3d_drag response entities (commit payload)
    // T-PERF2: single-in-flight latest-wins sketch3d_drag
    int  dragGen_ = 0;
    bool dragInFlight_ = false;
    bool dragPending_ = false;
    nlohmann::json dragPendingExtra_;
    void queueSketch3dDrag(AppState& st, nlohmann::json extra);
    void kickSketch3dDragAsync(AppState& st);

    // per-frame hover
    std::string hovHandle_;
    std::string hovEnt_;
    // GUI-picking overhaul: see inSketchEntityHot(). Defaults true (safe before the first frame).
    bool entityHoverHot_ = true;
};

}  // namespace ui
