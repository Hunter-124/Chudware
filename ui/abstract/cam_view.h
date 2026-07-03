#pragma once
// cam_view.h — framework-agnostic view-model for the T-U3 CAM workspace panel.
// Follows the ui/abstract/ convention: no ImGui/GL/GLFW includes; a pure struct read by the
// ui/imgui backend and (in future) any other renderer. See docs/UI_PATTERN_LIBRARY.md §1.
//
// State mirrors the C++ core's cam_* RPC surface (cam_tree / cam_generate / cam_simulate /
// cam_post_dialects / cam_post / cam_setup_sheet).  Mutation goes through the worker RPC seam
// in cam_panel.cpp; this header is read-only from the renderer's point of view.

#include <string>
#include <vector>

namespace aicad::ui {

// ---- per-op tree row --------------------------------------------------------

struct CamOpView {
    std::string id;
    std::string name;       // user-visible name (may be empty → fall back to type)
    std::string type;       // e.g. "pocket2d", "adaptive2d", "drill"
    int    tool_number = 0;
    bool   suppressed  = false;
    bool   arc_blend_linking = true;  // M2/M4 backend field; shown in op detail
};

// ---- per-setup tree row -----------------------------------------------------

struct CamSetupView {
    std::string id;
    std::string name;
    std::string machine;          // "mill3" | "mill5" | "lathe" | …
    std::vector<CamOpView> ops;
};

// ---- tool table row ---------------------------------------------------------

struct CamToolView {
    int    number   = 0;
    std::string name;
    std::string type;           // "endmill_flat" | "ball" | "drill" | …
    double diameter = 0.0;      // mm
};

// ---- generate warning (from cam_generate warnings[]) ------------------------

struct CamGenWarning {
    std::string op_id;      // which operation; empty = job-level
    bool        is_error = false;
    std::string text;
};

// ---- simulate result --------------------------------------------------------

enum class CamSimState { None, Running, Ok, Error };

struct CamSimView {
    CamSimState state = CamSimState::None;
    double stock_volume_mm3    = 0;
    double removed_volume_mm3  = 0;
    double remaining_volume_mm3 = 0;
    double machined_fraction   = 0;   // 0..1
    double cycle_time_s        = 0;
    double cut_time_s          = 0;
    double rapid_time_s        = 0;
    int    gouges              = 0;
    double max_gouge_mm        = 0;
    int    rapid_collisions    = 0;
    int    holder_collisions   = -1;  // -1 = not checked (T-M3 unmerged / check_holder=false)
    std::vector<std::string> warnings;
    std::string error_text;
};

// ---- post / output ----------------------------------------------------------

struct CamPostView {
    std::vector<std::string> dialects;  // from cam_post_dialects
    int    selected_idx = 0;
    std::string gcode;
    std::string gcode_path;     // written temp file path (shown to user)
    int    line_count  = 0;
    std::string setup_sheet_path;
};

// ---- top-level job model ----------------------------------------------------

enum class CamJobState {
    Empty,      // no job yet
    Loaded,     // tree available, no toolpaths
    Generated,  // toolpaths generated (backplot ready)
    Simulated,  // sim result available
};

struct CamViewModel {
    std::string job_doc_id;         // "" = no job
    std::string job_name;
    std::string job_post;           // active post dialect
    int         revision   = -1;
    CamJobState job_state  = CamJobState::Empty;

    std::vector<CamSetupView> setups;
    std::vector<CamToolView>  tools;
    std::vector<CamGenWarning> gen_warnings;  // from last cam_generate

    CamSimView  sim;
    CamPostView post;

    // UI selection
    int sel_setup = 0;
    int sel_op    = -1;

    // Status / last action text (shown in footer)
    std::string status;
    bool        status_is_error = false;
};

}  // namespace aicad::ui
