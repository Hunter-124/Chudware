// core/agent/complexity.h — deterministic task-complexity router.
//
// Classifies a design request into a harness structure BEFORE any LLM planner
// call, so simple parts skip plan-decomposition and run as a single solo model
// loop, while multi-part / assembly / mechanism requests get the full plan stage.
// Pure string heuristics (no LLM, no I/O) -> deterministic + unit-testable.
#pragma once

#include <string>

namespace aicad {
namespace agent {

enum class Complexity {
    Solo,      // one primitive/standard part -> skip planner, single model loop
    Standard,  // a normal multi-feature part -> normal plan stage
    Complex,   // assembly / mechanism / system -> plan stage (+ heavier structure)
};

struct ComplexityVerdict {
    Complexity level;
    std::string reason;  // short human-readable justification (shown in the GUI)
    const char* label() const {
        switch (level) {
            case Complexity::Solo: return "solo";
            case Complexity::Standard: return "standard";
            case Complexity::Complex: return "complex";
        }
        return "standard";
    }
};

// Classify a natural-language design request. Deterministic.
ComplexityVerdict classify_complexity(const std::string& request);

// Recommended execution structure derived from complexity (pure, deterministic).
// Solo tasks stay in-process; Standard fans out a subagent; Complex becomes a
// DAG, or a Company when the request is known to cross disciplines.
enum class ExecStructure {
    Solo,      // single in-process model loop
    Subagent,  // one delegated worker
    Dag,       // multi-node plan graph
    Company,   // multi-discipline company of agents
};

// Map Complexity -> ExecStructure. When level is Complex and cross_discipline
// is true, prefer Company over Dag. Pure switch, no I/O.
ExecStructure recommend_structure(Complexity level, bool cross_discipline = false);

// Stable lowercase label: "solo" | "subagent" | "dag" | "company".
const char* structure_label(ExecStructure s);

}  // namespace agent
}  // namespace aicad
