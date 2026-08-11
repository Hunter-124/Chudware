// Chudware ribbon command table — C14.
// ribbon_def.cpp owns the static table; ribbon.cpp and app.cpp call getRibbonCmds().
// Later cards APPEND to ribbon_def.cpp (never edit existing rows).
#pragma once

namespace ui {

enum class CommandCapability {
    Mutate,   // default and fail-closed for every unclassified/new command
    Inspect,  // view/display/measure only
    Export,   // writes a copy; never mutates the open document
};

struct RibbonCmd {
    const char* cmd_id;   // snake_case unique command identifier
    const char* label;    // display label (short, ≤12 chars fits big button)
    const char* icon;     // icon name without @32/@20 suffix
    const char* tab;      // "Features" | "Sketch" | "Evaluate" | "Simulation"
    const char* group;    // group label
    bool        big;      // true = 32px icon over label; false = 20px + label on right
    const char* sub_of;   // nullptr for top-level; parent cmd_id for dropdown sub-commands
    bool        stretch;  // true = schema-reserved but not wired in v1 (rendered disabled)
    CommandCapability capability = CommandCapability::Mutate;
};

// Returns pointer to the static command table and sets count.
const RibbonCmd* getRibbonCmds(int& count);
// Unknown ids are Mutate. Adding a command without classifying it can only
// over-block read-only mode, never silently unlock editing.
CommandCapability ribbonCommandCapability(const char* cmdId);

}  // namespace ui
