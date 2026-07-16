// Evaluate dialogs (C19 + Evaluate overhaul): wires the ribbon Evaluate-tab commands to the
// LIVE worker eval_* methods (worker_rpc.md §2). Read-only measurements open small result windows
// (Mass Properties, Measure, Section Props, Check, Interference); field analyses (Draft, Thickness,
// Curvature) return an {aicm_path} overlay that is loaded into the viewport with the result colormap
// + legend on (reusing the C19 renderer scalar pass).
//
// Measure is live (re-evals on selection change). Mass Properties seeds density from the document
// material key, exposes COM / section I / polar J / axial k, and drives viewport markers + body
// translucency via the overlay accessors below.
#pragma once

#include <array>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

class EvaluateDialogs {
public:
    // Handle a ribbon Evaluate command. Returns true if consumed. C39: the eval_* call runs
    // async (st.rpc) — the result window opens immediately showing "Computing..." and fills
    // in when the result drains; field analyses load the overlay + colormap/legend then.
    bool handleCommand(AppState& st, const std::string& cmdId);

    // Draw the currently-open result window (no-op when none is open). Also drives live Measure
    // re-eval and mass-design section fetches.
    void draw(AppState& st);

    // Which result window is currently open ("mass","measure","section",...; empty = none). Used by
    // the AI_CAD_SHOT=multidoc scenario to assert that a measurement opened on one document does not
    // bleed into another's (per-document EvaluateDialogs).
    const std::string& openWindowKind() const { return openWindow_; }

    // Viewport overlay batches (world-space). Consumed by viewport_panel after sim overlays.
    const std::vector<float>& overlaySegments() const { return overlaySegs_; }
    const std::vector<std::pair<std::array<float, 3>, std::string>>& overlayLabels() const {
        return overlayLabels_;
    }
    // Whole-body alpha to apply while mass markers are visible (1.0 = leave alone).
    float requestedBodyAlpha() const { return massBodyAlpha_; }

private:
    void runEval(AppState& st, const std::string& method, const nlohmann::json& extra);
    void runFieldEval(AppState& st, const std::string& method, const nlohmann::json& extra,
                      const std::string& legendTitle, const std::string& units);
    void clearOverlays();
    void rebuildMeasureOverlays(AppState& st);
    void rebuildMassOverlays(AppState& st);
    void tickLiveMeasure(AppState& st);
    void tickMassDesignSections(AppState& st);
    void seedMassFromDoc(AppState& st);

    std::string openWindow_;     // which result window is showing ("mass","measure",…); empty=none
    std::string title_;
    nlohmann::json result_;      // last eval result object
    std::string errorText_;      // set when the eval failed
    bool pending_ = false;       // C39: an eval is in flight (window shows "Computing...")
    int  gen_ = 0;               // C39: bumped per eval; stale callbacks are ignored

    // Mass Properties material override: combo index into the density preset table + the live
    // density (g/cm3) sent to eval_mass. Changing either re-runs the evaluation.
    // massMaterial_ 0 = Document material (when key non-empty), else first preset.
    int   massMaterial_ = 0;
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

    // Measure accumulation + live mode.
    std::vector<std::string> measureLog_;
    int   measureLoggedGen_ = -1;
    std::string lastMeasureRefs_;
    double lastMeasureFireT_ = -1e9;

    // Section props plane: 0 top, 1 front, 2 right, 3 selected face.
    int sectionPlane_ = 0;

    // Symmetry plane: 0 front, 1 top, 2 right.
    int symmetryPlane_ = 0;

    // Viewport overlays (world XYZ segments + labels).
    std::vector<float> overlaySegs_;
    std::vector<std::pair<std::array<float, 3>, std::string>> overlayLabels_;
};

}  // namespace ui
