#pragma once
// sim_view.h — framework-agnostic view-model for the T-U6 Simulation Studies panel.
// Follows the ui/abstract/ convention: no ImGui/GL/GLFW includes — pure structs read by
// ui/imgui/src/ui/sim_panel.cpp. See docs/UI_PATTERN_LIBRARY.md §1.
//
// Backs up against SIM_AUDIT.md findings:
//   - run_fea_static/modal: C++ plugins, only reachable via the full MCP worker process.
//   - run_cfd_flow (LBM):   Cd IS UNCALIBRATED BY DESIGN — surfaced via the narration text
//     from describe_results verbatim; sim_panel NEVER hardcodes calibration strings.
//   - describe_results narrates whatever verdict fields exist — panel renders them only.
//
// Study records come from sim_study_list RPC (reads doc["studies"] array written by
// _persist_study after each solve). The narration text from describe_results is shown
// verbatim in a collapsible section so the numbers-are-authoritative rule holds.

#include <string>
#include <vector>

namespace aicad::ui {

// ---- loading state for the study list -----------------------------------------

enum class SimPanelState {
    Idle,       // no doc loaded / worker not ready
    Loading,    // sim_study_list RPC in-flight
    Ready,      // study list populated (may be empty)
    Error,      // RPC or engine error
};

// ---- run state for a re-run action --------------------------------------------

enum class SimRunState {
    Idle,
    Running,    // async RPC in-flight — show spinner + "Running…"
    Done,       // result received — list will refresh on next tick
    Error,      // error text in run_error
};

// ---- one study row (mirrors _persist_study record fields) ---------------------

struct SimStudyRow {
    std::string id;
    std::string type;       // "fea_static" | "fea_modal" | "cfd_flow" | …
    std::string name;       // human-readable, e.g. "Static stress (steel)"
    std::string engine;     // "cpp" | "cpp-lbm" | …

    // Result summary scalars — rendered when present; empty/NaN = absent (not shown)
    double max_von_mises_mpa = -1.0;  // fea_static: <0 = absent
    double max_disp_mm       = -1.0;  // fea_static
    double min_fos           = -1.0;  // fea_static: <0 = absent
    std::vector<double> freqs_hz;     // fea_modal

    // CFD scalars
    double drag_n    = -1.0;
    double lift_n    = -1.0;
    double cd        = -1.0;
    double reynolds  = -1.0;

    // Convergence / calibration warnings from the backend response
    // (run_cfd_flow: "converged" field; warning text from result record)
    bool   converged        = true;   // CFD: false → "not converged" badge
    bool   cd_uncalibrated  = false;  // CFD LBM: always true per SIM_AUDIT; drives caveat badge
    std::string warning;              // optional backend warning text (e.g. under-resolved)

    // Narration text from describe_results (lazy-fetched on expand)
    std::string narration;            // empty until narration_loaded
    bool narration_loading = false;
    bool narration_loaded  = false;
    bool narration_error   = false;

    // Re-run action state (per-study)
    SimRunState run_state  = SimRunState::Idle;
    std::string run_error;
};

// ---- top-level panel view-model -----------------------------------------------

struct SimPanelModel {
    SimPanelState state    = SimPanelState::Idle;
    std::string   errorMsg;
    std::vector<SimStudyRow> studies;

    // Selection (which row is expanded in the detail/narration section)
    int sel_idx = -1;

    // Cache invalidation keys
    std::string cache_doc_id;
    int         cache_revision = -1;

    // Status line (passed to PanelScaffoldState.statusText)
    std::string status;
    bool        status_is_error = false;
};

}  // namespace aicad::ui
