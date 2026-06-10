// Evaluate dialogs (C19): wires the ribbon Evaluate-tab commands to the LIVE worker eval_*
// methods (worker_rpc.md §2; reports.md §C12). Read-only measurements open small result windows
// (Mass Properties, Measure, Section Props, Check, Interference); field analyses (Draft, Thickness,
// Curvature) return an {aicm_path} overlay that is loaded into the viewport with the result colormap
// + legend on (reusing the C19 renderer scalar pass).
#pragma once

#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

class EvaluateDialogs {
public:
    // Handle a ribbon Evaluate command. Returns true if consumed. Synchronous worker call (eval_*
    // are fast); on field analyses it loads the overlay + enables the colormap/legend.
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
};

}  // namespace ui
