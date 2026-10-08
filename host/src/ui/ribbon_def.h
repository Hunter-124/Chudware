// Chudware ribbon command table — C14.
// ribbon_def.cpp owns the static table; ribbon.cpp and app.cpp call getRibbonCmds().
// Later cards APPEND to ribbon_def.cpp (never edit existing rows). A later row with the same tab +
// cmd_id replaces an earlier one; ribbon_def.cpp kRetiredRows hides a row with no replacement.
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
// False for a row that is superseded (a later row has the same tab + cmd_id) or retired. Every
// surface that lists table rows per tab skips inactive rows; `index` is into getRibbonCmds().
bool ribbonRowActive(int index);
// Unknown ids are Mutate. Adding a command without classifying it can only
// over-block read-only mode, never silently unlock editing.
CommandCapability ribbonCommandCapability(const char* cmdId);

// Simulate study tabs: how a row applies to the selected study. `studyTypeKey` is SimManager's
// study type key ("static", "modal", "buckling", "contact", "freq_response", "fatigue", "thermal",
// "thermal_fea", "flow", "motion", "topology"); "" = no study selected; nullptr = no study
// context (outside the Simulate study flow). Disabled sets `*reason` to the tooltip text. Rows
// that configure one study type only (e.g. Buckling "Modes" on the Static tab) are Hidden unless
// a study of that type is selected.
enum class RibbonStudyGate { Enabled, Disabled, Hidden };
RibbonStudyGate ribbonStudyGate(const char* cmdId, const char* studyTypeKey, const char** reason);

}  // namespace ui
