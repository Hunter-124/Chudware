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

struct AiEffortModel {
    EffortState state = EffortState::Idle;
    std::string level = "standard";         // active preset
    std::string mode  = "simple";           // simple | advanced
    std::vector<EffortLevel>    levels;     // dial order (eco → maximum)
    std::vector<EffortKnobMeta> knobMeta;   // grouped knob catalog
    std::vector<EffortTier>     tiers;
    int selectedTier = 0;                   // advanced-mode tier being edited
    std::string statusText;
    std::string errorMsg;
};

}  // namespace aicad::ui
