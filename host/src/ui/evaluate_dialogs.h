// Evaluate dialogs (C19 + Evaluate overhaul): wires the ribbon Evaluate-tab commands to the
// LIVE worker eval_* methods (worker_rpc.md §2). Read-only measurements open small result windows
// (Mass Properties, Measure, Section Props, Check, Interference); field analyses (Draft, Thickness,
// Curvature) return an {aicm_path} overlay that is loaded into the viewport with the result colormap
// + legend on (reusing the C19 renderer scalar pass).
//
// Measure is live (re-evals on selection change). Mass Properties density DERIVES from the document
// material key (doc.material.key via set_material) by default; the Material field WRITES set_material
// so changing it updates the part's active material. A density field is the only local advanced
// override. Exposes COM / section I / polar J / axial k, and drives viewport markers + body
// translucency via the overlay accessors below. Interference view tints bodies + overlap meshes;
// Measure color-codes refs with viewport highlights + callouts.
// Wave 3: massMarkers_ carry COM + bending-I value chips (unit-aware); sectionPlanes_ expose three
// draggable orthogonal cutting planes with live section-prop re-eval. All analysis controls use
// resizable floating command windows with persistent geometry, built from the ui::kit panel
// scaffold (scroll body + pinned action footer), kit_form fields and kit result cards.
#pragma once

#include <array>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// Per-ref viewport highlight for Measure (face wash / edge polyline / vertex point).
// kind matches render::PickKind: 1=Face, 2=Edge, 3=Vertex.
struct EvalHighlight {
    uint64_t faceKey = 0;              // (body_id<<32)|face_id when kind==Face
    int bodyId = 0;
    int kind = 0;
    std::array<float, 3> point{};      // vertex position (or face/edge anchor fallback)
    std::vector<float> polyline;       // flat world xyz for edges (consecutive samples)
    float rgba[4] = {1, 1, 1, 1};
};

// In-viewport reading chip (world pos + unit-aware text + matching palette color).
struct EvalCallout {
    std::array<float, 3> pos{};
    std::string text;
    float rgba[4] = {1, 1, 1, 1};
};

// Interference Detection viewport state: translucent bodies, per-pair body washes + overlap mesh.
struct InterferenceView {
    bool active = false;
    struct Pair {
        int bodyA = 0;                 // 0 = unresolved (skip body wash)
        int bodyB = 0;
        float rgbaA[4] = {1, 1, 1, 1};
        float rgbaB[4] = {1, 1, 1, 1};
        std::vector<float> overlapTris;  // flat xyz triples for Overlays::drawTriangles
        float overlapRgba[4] = {1, 1, 1, 1};
    };
    std::vector<Pair> pairs;
};

// Mass Properties viewport value chip (COM + per-plane bending I / polar J). Viewport draws these
// as labelled text at world pos with hover tooltips; NOT measure overlayLabels_.
struct MassMarker {
    std::array<float, 3> pos{};
    std::string label;
    std::string tooltip;
    float rgba[4] = {1, 1, 1, 1};
};

// One of the three orthogonal section cutting planes shown while Section Props is open.
// origin = centroid + normal * offset (world mm). Viewport draws the translucent quad + handles
// drag along normal → dragSectionPlane().
struct SectionPlaneView {
    std::array<float, 3> origin{};   // world mm: centroid shifted by current offset along normal
    std::array<float, 3> normal{};   // unit
    std::array<float, 3> uAxis{}, vAxis{};  // in-plane axes for the quad
    float halfSize = 0.f;            // quad half-extent, world mm (from bbox)
    float rgba[4] = {1, 1, 1, 1};
    int index = 0;
    std::string label;
    bool active = false;
};

class EvaluateDialogs {
public:
    // Handle a ribbon Evaluate command. Returns true if consumed. C39: the eval_* call runs
    // async (st.rpc) — the result window opens immediately showing "Computing..." and fills
    // in when the result drains; field analyses load the overlay + colormap/legend then.
    bool handleCommand(AppState& st, const std::string& cmdId);

    // Draw the currently-open result window (no-op when none is open). Also drives live Measure
    // re-eval and mass-design section fetches.
    void draw(AppState& st);
    // Invalidate before viewport overlay consumption, including selections changed this frame.
    void prepareFrame(AppState& st);

    // Which exclusive result window is open ("measure","check",...; empty = none). Mass and
    // Section use independent flags (massOpen_/sectionOpen_) so they can coexist.
    const std::string& openWindowKind() const { return openWindow_; }

    // Viewport overlay batches (world-space). Consumed by viewport_panel after sim overlays.
    // Composes measure segs + mass COM/axis segs so both can coexist.
    const std::vector<float>& overlaySegments() const;
    const std::vector<std::pair<std::array<float, 3>, std::string>>& overlayLabels() const {
        return overlayLabels_;
    }
    const std::vector<EvalHighlight>& overlayHighlights() const { return overlayHighlights_; }
    const std::vector<EvalCallout>& overlayCallouts() const { return overlayCallouts_; }
    const InterferenceView& interferenceView() const { return interferenceView_; }
    // Whole-body alpha while mass markers, section planes, or interference are visible.
    float requestedBodyAlpha() const;

    // Mass Properties value chips (COM + bending I / polar J). Viewport-owned draw; dialogs own data.
    const std::vector<MassMarker>& massMarkers() const { return massMarkers_; }
    // Three orthogonal section planes while Section Props is open.
    const std::vector<SectionPlaneView>& sectionPlanes() const { return sectionPlanes_; }
    // Viewport drag: move plane `idx` by `deltaAlongNormal` (world mm), debounce re-eval.
    void dragSectionPlane(AppState& st, int idx, float deltaAlongNormal);
    // True while the Section Properties window is open (viewport enables plane hit-test / drag).
    bool sectionDragActive() const { return sectionOpen_; }

private:
    void runEval(AppState& st, const std::string& method, const nlohmann::json& extra);
    void runMassEval(AppState& st, const nlohmann::json& extra);
    void runFieldEval(AppState& st, const std::string& method, const nlohmann::json& extra,
                      const std::string& legendTitle, const std::string& units);
    void clearOverlays();
    void rebuildMeasureOverlays(AppState& st);
    void rebuildMassOverlays(AppState& st);
    void rebuildInterferenceView(AppState& st);
    void clearFieldColormap(AppState& st);
    void tickLiveMeasure(AppState& st);
    void tickMassDesignSections(AppState& st);
    void tickSectionDrag(AppState& st);
    void syncMassMaterial(AppState& st);
    void seedMassFromDoc(AppState& st);
    void initSectionSession(AppState& st);
    void fireSectionEval(AppState& st, int idx);   // one plane; -1 reserved
    void fireAllSectionEvals(AppState& st);
    void rebuildSectionPlaneViews(AppState& st);
    void applySectionOffset(AppState& st, int idx, float newOffset, bool scheduleEval);
    void drawMassWindow(AppState& st);
    void drawSectionWindow(AppState& st);
    void drawExclusiveWindow(AppState& st);
    void syncDocument(AppState& st);
    void drawMeasure(AppState& st);
    void runGeometryAnalysis(AppState& st);
    void drawGeometryAnalysis(AppState& st);
    void drawCheck(AppState& st);
    void drawInterference(AppState& st);
    void drawSymmetry(AppState& st);
    void drawZebra(AppState& st);
    void drawFieldTool(AppState& st, const std::string& kind);   // draft / thickness / curvature
    void runFieldTool(AppState& st, const std::string& kind);    // the footer's Update action
    void drawExclusiveFooter(AppState& st, const std::string& kind);

    // Exclusive tools (measure/check/interference/draft/thickness/curvature/zebra/symmetry).
    // Mass and Section are independent so both can be open at once.
    std::string openWindow_;
    std::string title_;
    nlohmann::json result_;      // last exclusive-tool eval result
    std::string errorText_;      // exclusive-tool error
    bool pending_ = false;
    int  gen_ = 0;
    std::string evalDocId_;
    int evalDocRevision_ = -1;

    // Independent Mass Properties window + its own result slot (survives exclusive tools).
    bool massOpen_ = false;
    nlohmann::json massResult_ = nlohmann::json::object();
    std::string massErrorText_;
    bool massPending_ = false;
    int  massGen_ = 0;

    // Independent Section Properties window.
    bool sectionOpen_ = false;
    std::string sectionErrorText_;


    // Mass Properties material: default density follows the active part material (doc.material.key).
    // massCustomDensity_ is an explicit advanced override that does NOT change the part material.
    // Changing the Material combo writes set_material (and derived set_appearance) to the doc.
    bool  massCustomDensity_ = false;
    float massDensity_  = 7.85f;
    std::string massDocKey_;
    std::string massDocName_;

    // Mass design checkboxes (viewport + dialog).
    bool showCom_ = true;
    bool showI_ = true;
    bool showJ_ = true;
    bool showK_ = true;
    float massBodyAlpha_ = 1.0f;

    // Cached design-section results keyed by principal axis index 0..2 (null = not yet).
    nlohmann::json designSections_ = nlohmann::json::array({nullptr, nullptr, nullptr});
    int designSectionGen_ = 0;
    int designSectionsPending_ = 0;
    bool designSectionsDirty_ = false;

    // Live measure: selection identity includes unresolved picks so every change invalidates.
    std::vector<std::string> measureLog_;
    std::string lastMeasureRefs_;
    bool measureResolving_ = false;
    bool measureDirty_ = false;
    double lastMeasureChangeT_ = -1e9;

    // Section props: 3 orthogonal planes through centroid with per-plane offset along normal.
    // (Legacy sectionPlane_ combo removed — 3-card layout replaces it.)
    float sectionCentroid_[3] = {0.f, 0.f, 0.f};
    float sectionOffset_[3] = {0.f, 0.f, 0.f};
    float sectionHalfSize_ = 50.f;          // ±offset range + quad half-extent scale source
    double sectionDragLastT_ = -1e9;
    int sectionPendingIdx_ = -1;            // plane awaiting debounced re-eval (-1 = none)
    int sectionEvalGen_ = 0;
    int sectionPendingCount_ = 0;
    nlohmann::json sectionResults_ = nlohmann::json::array({nullptr, nullptr, nullptr});

    // Geometry analysis limits (model mm). Zero means use the engine's per-body, size-scaled
    // default (the field shows "Auto").
    float geometryMinArea_ = 0.f, geometryMinEdge_ = 0.f;
    float geometrySliverRatio_ = 0.f, geometryGapTol_ = 0.f;
    bool geometryDiscontinuities_ = false;
    bool geometryLimitsOpen_ = false;          // "Limits used" card (collapsed by default)
    bool geometryKindOpen_[7] = {true, true, true, true, true, true, true};  // findings cards
    bool measureHistoryOpen_ = false;          // "Recent readings" card
    bool massTensorOpen_ = false;              // "Mass inertia tensor" card
    // Symmetry plane: 0 front, 1 top, 2 right.
    int symmetryPlane_ = 0;

    // Field-analysis dock params (draft / thickness / curvature).
    int   draftPull_ = 2;           // 0=X 1=Y 2=Z 3=-Z
    float draftAngleDeg_ = 1.0f;
    int   curvatureKind_ = 0;       // 0=mean 1=gaussian 2=max 3=min
    float thicknessRes_ = 0.5f;     // optional resolution hint (mm)

    // Interference member subset; inventory comes only from an unfiltered result.
    std::vector<std::string> interferencePartNames_;
    std::vector<char> interferencePartChecked_;  // parallel to names; 1 = included

    // Viewport overlays (world XYZ segments + labels + highlights + callouts).
    // overlaySegs_ holds exclusive-tool (measure) segments; massSegs_ holds COM/axis segments.
    // overlaySegments() composes both into composedSegs_.
    std::vector<float> overlaySegs_;
    std::vector<float> massSegs_;
    mutable std::vector<float> composedSegs_;
    std::vector<std::pair<std::array<float, 3>, std::string>> overlayLabels_;
    std::vector<EvalHighlight> overlayHighlights_;
    std::vector<EvalCallout> overlayCallouts_;
    InterferenceView interferenceView_;

    // Mass COM / I / J value chips (separate from measure-only overlayLabels_).
    std::vector<MassMarker> massMarkers_;
    // Live section cutting planes (populated while sectionOpen_).
    std::vector<SectionPlaneView> sectionPlanes_;
};

}  // namespace ui
