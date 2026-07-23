// core/cam/post.h — the post-processor: a data-driven NC dialect model + the
// engine that renders a Setup's toolpaths into a controller-specific G-code/M-code
// program. Dialects (GRBL, LinuxCNC, Fanuc, Haas, Mach3, Marlin, Siemens 840D,
// Centroid/Tormach) are PostConfig presets authored clean-room from public G-code
// references — no vendor posts are copied.
#pragma once

#include <string>
#include <vector>

#include "cam/model.h"

namespace chudware {
namespace cam {

// Number formatting for one axis/word class.
struct NumFmt {
    int decimals = 3;
    bool trailing_zeros = true;   // 1.500 vs 1.5
    bool leading_zero = true;     // 0.5 vs .5
    bool plus_sign = false;       // +1.5
};

// A controller dialect. Everything the engine needs to format output is here, so
// adding a controller is data, not code.
struct PostConfig {
    std::string id;            // "grbl", "fanuc", ...
    std::string name;
    std::string description;

    // program framing
    bool program_numbers = false;   // O1234 program id
    bool sequence_numbers = false;  // N10 N20 line numbers
    int seq_start = 10, seq_inc = 10;
    bool percent_wrap = false;      // leading/trailing '%'
    std::string comment_open = "(";
    std::string comment_close = ")";
    bool comments_allowed = true;
    std::string program_begin;      // raw lines emitted at the very top (after %/O)
    std::string program_end;        // raw lines before M30/%
    std::string units_metric = "G21";
    std::string units_imperial = "G20";
    std::string abs_mode = "G90";
    std::string plane_xy = "G17";

    // motion
    bool arcs_supported = true;
    bool arcs_ijk = true;           // I/J/K incremental centre vs R radius
    bool arc_full_circle_ijk = true;
    bool r_for_arcs = false;        // emit R instead of IJK
    double max_arc_radius = 0;      // 0 = unlimited; else split big arcs to lines
    bool modal_motion = true;       // omit repeated G0/G1
    bool modal_feed = true;         // omit repeated F
    std::string rapid = "G0";
    std::string linear = "G1";
    std::string cw = "G2";
    std::string ccw = "G3";

    // spindle / coolant
    std::string spindle_cw = "M3";
    std::string spindle_ccw = "M4";
    std::string spindle_off = "M5";
    std::string coolant_flood = "M8";
    std::string coolant_mist = "M7";
    std::string coolant_off = "M9";
    bool spindle_speed_word = true; // S word
    bool wait_for_spindle = false;  // dwell after spindle on
    double spindle_dwell_s = 0;

    // tool change
    std::string tool_change = "M6";     // "" = none (single-tool controllers)
    bool tool_word_before_change = true;// T then M6 (true) vs M6 T (false)
    std::string tool_change_extra;      // e.g. retract macro lines
    bool length_offset = true;          // emit G43 H on tool change
    std::string length_offset_code = "G43";

    // work offsets
    bool work_offset = true;            // emit G54..
    // canned cycles
    bool canned_drill = true;           // use G81/G83/... (false -> long-hand)
    std::string cycle_drill = "G81";
    std::string cycle_peck = "G83";
    std::string cycle_chipbreak = "G73";
    std::string cycle_tap = "G84";
    std::string cycle_rigidtap = "G84";
    std::string cycle_ream = "G85";
    std::string cycle_bore = "G86";
    std::string cycle_cancel = "G80";
    std::string cycle_retract_g98 = "G98";  // return to initial
    std::string cycle_retract_g99 = "G99";  // return to R plane
    std::string dwell = "G4";
    std::string dwell_word = "P";           // P seconds vs P milliseconds
    bool dwell_seconds = true;

    // cutter comp
    bool comp_supported = true;
    std::string comp_left = "G41";
    std::string comp_right = "G42";
    std::string comp_off = "G40";

    // program end
    std::string program_stop = "M30";

    // laser/plasma/waterjet specifics
    bool laser_mode = false;            // S = power, M3/M5 gate the beam
    std::string torch_on = "M3";
    std::string torch_off = "M5";
    double power_max = 1000;            // S value at 100% power

    // number formats
    NumFmt xyz{3, true, true, false};
    NumFmt feed{1, true, true, false};
    NumFmt rpm{0, false, true, false};
    NumFmt arc{3, true, true, false};

    // turning / lathe
    bool lathe = false;
    bool diameter_mode = true;          // X = diameter (true) vs radius
    std::string css_on = "G96";
    std::string css_off = "G97";
    std::string css_clamp = "G50";      // spindle max-rpm clamp emitted before G96 (Fanuc/Haas: G50 S<max>)
    std::string feed_per_rev = "G99";   // turning feed mode
    std::string feed_per_min = "G98";

    json to_json() const;
    static PostConfig from_json(const json&);
};

// The dialect registry: builtin presets + lookups.
const PostConfig& post_dialect(const std::string& id);  // throws if unknown
std::vector<std::string> post_dialect_ids();
json post_dialects_json();   // [{id,name,description,kinds:[...]}] for the GUI/MCP

// Options influencing a post run (overridable from the Job).
struct PostOptions {
    bool sequence_numbers = false;     // force line numbers on
    bool comments = true;
    bool metric = true;
    std::string program_name = "Chudware";
    int program_number = 1000;
    bool separate_files_per_setup = false;
    json overrides = json::object();   // PostConfig field overrides
};

struct PostResult {
    bool ok = true;
    std::string error;
    std::string gcode;                 // the NC program text
    int line_count = 0;
    int tool_changes = 0;
    double est_time_s = 0;             // summed from toolpath stats
    std::vector<std::string> warnings;
    json to_json() const;
};

// Post one setup (its operations must already be generated into toolpaths).
PostResult post_setup(const Setup&, const ToolLibrary&, const std::string& dialect_id,
                      const std::vector<Toolpath>&, const PostOptions& = {});

// Post a whole job (one program, or concatenated setups).
PostResult post_job(const Job&, const std::vector<std::vector<Toolpath>>& per_setup,
                    const PostOptions& = {});

// ── setup sheet / tool list (HTML + plain text) ──────────────────────────────
struct SheetResult {
    std::string html;
    std::string text;
    json to_json() const;
};
SheetResult setup_sheet(const Job&, const std::vector<std::vector<Toolpath>>& per_setup,
                        const std::string& dialect_id);

}  // namespace cam
}  // namespace chudware
