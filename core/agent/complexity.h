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

}  // namespace agent
}  // namespace aicad
