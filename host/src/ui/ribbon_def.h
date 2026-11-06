// Chudware ribbon command table — C14.
// ribbon_def.cpp owns the static table; ribbon.cpp and app.cpp call getRibbonCmds().
// Later cards APPEND to ribbon_def.cpp (never edit existing rows). A later row with the same tab +
// cmd_id replaces an earlier one; ribbon_def.cpp kRetiredRows hides a row with no replacement.
#pragma once

#include <string>

namespace ui {

enum class CommandCapability {
    Mutate,   // default and fail-closed for every unclassified/new command
    Inspect,  // view/display/measure only
    Export,   // writes a copy; never mutates the open document
};

// Immutable, inexpensive snapshot used by every command surface and the dispatch boundary.
// Document-derived facts are populated from cached state, never resolved while drawing a row.
enum class CommandDocKind { None, Part, Assembly, Drawing, Schematic, PCB };

struct CommandContext {
    CommandDocKind activeDocKind = CommandDocKind::None;
    std::string activeDocId;
    int activeDocRevision = -1;  // matching mechanical mirror revision, otherwise unknown
    bool activeMechanicalReady = true;  // false while the active tab is not bound to its mirror
    int workspace = 0;
    bool inSimulation = false;
    bool sketch2dActive = false;
    bool sketch3dActive = false;
    bool sketch2dHasEntitySelection = false;
    bool sketch2dHasFixSelection = false;
    bool sketch3dHasSelection = false;
    bool sketch3dHasFixSelection = false;
    bool drawingViewSelected = false;
    bool hasOpenSchematic = false;
    bool hasOpenBoard = false;
    bool ecadHasPath = false;
    bool ecadHasProject = false;
    bool kicadStatusKnown = false;
    bool kicadAvailable = false;
    bool kicadSchematicAvailable = false;
    bool kicadPcbAvailable = false;
    bool kicadProjectAvailable = false;
    bool modelHasSolid = false;
    bool modelSolidKnown = true;
    bool sheetMetalHasBase = false;
    bool sheetMetalHasBends = false;
    bool sheetMetalHasValidFlat = false;
    // If an older or partially hydrated mirror lacks a status, absence of evidence is not
    // treated as a failed feature. Known-invalid and rolled-back features never set these.
    bool sheetMetalBaseStatusUnknown = false;
    bool sheetMetalBendStatusUnknown = false;
    bool sheetMetalFlatStatusUnknown = false;

    // Absence is meaningful only after a successful, current list. These flags describe
    // the active document's relevant requests, not unrelated capability/metadata probes.
    bool studyFactsCurrent = false;
    bool studyListPending = false;
    bool simCreationPending = false;
    std::string selectedStudyType;
    std::string topologyPhysics;
    bool hasModalStudy = false;
    bool hasBucklingStudy = false;
    bool hasContactStudy = false;
    bool hasFreqResponseStudy = false;
    bool hasThermalFeaStudy = false;
    bool hasFatigueStudy = false;
    bool hasMotionStudy = false;
    bool hasFlowStudy = false;
    bool hasThermalStudy = false;
    bool hasTopologyStudy = false;
    bool contactCapabilityKnown = false;
    bool contactCapabilityAvailable = false;
    bool vibrationCapabilityKnown = false;
    bool vibrationCapabilityAvailable = false;
    bool emAnalysisOpen = false;
    bool emCapabilityKnown = false;
    bool emCapabilityProbeFailed = false;
    bool emCapabilityAvailable = false;
    bool thermalCfdCapabilityKnown = false;
    bool thermalCfdCapabilityAvailable = false;
    bool topologyCapabilityKnown = false;
    bool topologyCapabilityAvailable = false;

    bool camHasJob = false;
    bool camHasSetup = false;
    bool camBusy = false;
    bool camHasSource = false;
    bool camHasCachedExtract = false;
};

struct CommandAvailability {
    bool visible = true;
    bool enabled = true;
    const char* reason = nullptr;  // static storage; never points into the snapshot
};

CommandAvailability commandAvailability(const char* cmdId, const CommandContext& context);

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
