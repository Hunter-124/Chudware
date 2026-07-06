#pragma once
// Framework-agnostic view-model for the T-W28 "AI Effort" settings section
// (harness effort: deliberation / parallelization / safety nets / context).
// Pure structs, no ImGui/GL/json includes — mirrors agent_settings_view.h.
//
// Mirrors the `view` JSON emitted by services/agent/effort.py (aicad.effort/1,
// contract §23): Simple-mode preset levels + per-tier resolved knobs, each
// carrying provenance ("shipped" | "preset:<level>" | "override") and
// warn-never-block advisories. The GUI edits a STRUCTURED model and reads /
// writes it exclusively through the effort.py CLI (get / set / validate) —
// resolution and warnings live in Python, never re-implemented here.
// Offline/shot mode seeds from a committed view fixture.
//
// T-DP12 extension: three modes — auto | simple | advanced.
//   auto     — default; the workflow_router (T-DP10) classifies each task and
//              self-selects the scaffold level; GUI shows informational tier
//              defaults + a description of the auto logic.
//   simple   — manual 4-level dial (Eco → Maximum).
//   advanced — full per-tier knob editor (deliberation / parallelization /
//              safety / context groups), exposed behind the toggle.

#include <string>
#include <utility>
#include <vector>

namespace aicad::ui {

enum class EffortState {
    Idle,     // not loaded
    Loading,  // reading via CLI
    Ready,    // view populated
    Error,    // load/parse failed (CLI unreachable — section shows a dim note)
};

// One Simple-mode level (Eco / Standard / Deliberate / Maximum).
struct EffortLevel {
    std::string id;      // "eco" | "standard" | "deliberate" | "maximum"
    std::string label;
    std::string hint;    // one-line cost/benefit hint
};

// Knob metadata (grouping + typing contract from effort.KNOBS).
struct EffortKnobMeta {
    std::string name;
    std::string group;   // deliberation | parallelization | safety | context
    std::string type;    // enum | bool | int | float | int_map
    std::string label;
    std::string doc;
    std::vector<std::string> choices;  // enum only
};

// One resolved knob value for one tier, with provenance.
struct EffortKnobValue {
    std::string name;
    std::string source;   // "shipped" | "preset:<level>" | "override"
    // Typed payload — exactly one is meaningful, per the meta type.
    std::string sval;     // enum
    bool        bval = false;
    double      nval = 0.0;  // int / float
    std::vector<std::pair<std::string, int>> mapval;  // int_map (class → attempts)
};

// One tier's fully-resolved profile.
struct EffortTier {
    std::string tier;                       // local-24k | local-48k | mid | frontier
    std::vector<EffortKnobValue> knobs;     // ordered like the meta list
    std::vector<std::string> warnings;      // warn-never-block advisories
};

// T-DP12: auto-mode router readout — one entry per tier showing the scaffold
// level the workflow_router defaults to for that tier (informational, not editable
// in auto mode).
struct AutoRouterTierDefault {
    std::string tier;           // "local-24k" | "local-48k" | "mid" | "frontier"
    std::string scaffoldLevel;  // "eco" | "standard" | "deliberate" | "maximum"
};

struct AiEffortModel {
    EffortState state = EffortState::Idle;
    std::string level = "standard";         // active preset (used in simple mode)
    std::string mode  = "auto";             // auto | simple | advanced (T-DP12)
    std::vector<EffortLevel>    levels;     // dial order (eco → maximum)
    std::vector<EffortKnobMeta> knobMeta;   // grouped knob catalog
    std::vector<EffortTier>     tiers;
    int selectedTier = 0;                   // advanced-mode tier being edited
    // T-DP12: auto-mode readout (populated from auto_router in the view JSON).
    std::string autoRouterDescription;
    std::vector<AutoRouterTierDefault> autoRouterDefaults;
    std::string statusText;
    std::string errorMsg;
};

}  // namespace aicad::ui
