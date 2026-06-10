// AI-CAD ribbon command table — C14.
// ribbon_def.cpp owns the static table; ribbon.cpp and app.cpp call getRibbonCmds().
// Later cards APPEND to ribbon_def.cpp (never edit existing rows).
#pragma once

namespace ui {

struct RibbonCmd {
    const char* cmd_id;   // snake_case unique command identifier
    const char* label;    // display label (short, ≤12 chars fits big button)
    const char* icon;     // icon name without @32/@20 suffix
    const char* tab;      // "Features" | "Sketch" | "Evaluate" | "Simulation"
    const char* group;    // group label
    bool        big;      // true = 32px icon over label; false = 20px + label on right
    const char* sub_of;   // nullptr for top-level; parent cmd_id for dropdown sub-commands
    bool        stretch;  // true = schema-reserved but not wired in v1 (rendered disabled)
};

// Returns pointer to the static command table and sets count.
const RibbonCmd* getRibbonCmds(int& count);

}  // namespace ui
