// Evaluate dialogs (C19): wires the ribbon Evaluate-tab commands to the LIVE worker eval_*
// methods (worker_rpc.md §2; reports.md §C12). Read-only measurements open small result windows
// (Mass Properties, Measure, Section Props, Check, Interference); field analyses (Draft, Thickness,
// Curvature) return an {aicm_path} overlay that is loaded into the viewport with the result colormap
// + legend on (reusing the C19 renderer scalar pass).
#pragma once

#include <string>
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

    // Draw the currently-open result window (no-op when none is open).
    void draw(AppState& st);

private:
    void runEval(AppState& st, const std::string& method, const nlohmann::json& extra);
    void runFieldEval(AppState& st, const std::string& method, const nlohmann::json& extra,
                      const std::string& legendTitle, const std::string& units);

    std::string openWindow_;     // which result window is showing ("mass","measure",…); empty=none
    std::string title_;
    nlohmann::json result_;      // last eval result object
    std::string errorText_;      // set when the eval failed
    bool pending_ = false;       // C39: an eval is in flight (window shows "Computing...")
    int  gen_ = 0;               // C39: bumped per eval; stale callbacks are ignored

    // Mass Properties material override: combo index into the density preset table + the live
    // density (g/cm3) sent to eval_mass. Changing either re-runs the evaluation.
    int   massMaterial_ = 0;     // 0 = Steel (matches the engine default 7.85)
    float massDensity_  = 7.85f;

    // Measure accumulation: each completed measurement is logged once (SolidWorks' Measure keeps a
    // running history). measureLoggedGen_ tracks the last eval gen appended so we log each result
    // exactly once even though the dialog redraws every frame.
    std::vector<std::string> measureLog_;
    int   measureLoggedGen_ = -1;
};

}  // namespace ui
