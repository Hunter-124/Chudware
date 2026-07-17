// core/agent/domain.h — deterministic task-DOMAIN classifier + tool-class registry.
//
// Layer 1 + Layer 3 of the task router (complexity.h is Layer 2). Classifies a
// request into an engineering DOMAIN (modeling / cam / ecad / …) and returns the
// SCOPED capability + context set for that domain, so an agent dispatched for a
// modeling task is not flooded with CAM/PCB/simulation tooling and context. This
// is how the harness stays lean on small tasks yet can widen for big ones.
//
// Pure string heuristics (no LLM, no I/O) -> deterministic + unit-testable.
#pragma once

#include <string>
#include <vector>

namespace aicad {
namespace agent {

enum class Domain {
    Modeling,    // solid/part modeling (sketch, extrude, revolve, fillet, …)
    Sheetmetal,  // sheet-metal flanges/hems/flat pattern
    Assembly,    // multi-component assemblies + mates
    Cam,         // manufacturing: setups, toolpaths, post
    Ecad,        // schematics / PCB / electronics
    Drawing,     // 2D engineering drawings
    Simulation,  // FEA / CFD / thermal
    Sourcing,    // standard parts, BOM, vendor sourcing
    Research,    // web/material/spec lookups, no geometry
};

struct DomainVerdict {
    Domain domain;
    std::string reason;
    const char* label() const;
};

// Classify a natural-language request into an engineering domain. Deterministic.
DomainVerdict classify_domain(const std::string& request);

// The SCOPED tool/capability classes for a domain — coarse capability tags a host
// maps to a concrete tool allowlist (keeps a modeling agent from loading CAM/ECAD
// tools). Always includes the domain's own class plus universally-safe read tags.
std::vector<std::string> tool_classes_for_domain(Domain d);

// The context cheatsheet names (shared/cheatsheets/<name>.md) relevant to a domain,
// so only on-topic reference material is injected. Empty = the role's default.
std::vector<std::string> context_sheets_for_domain(Domain d);

}  // namespace agent
}  // namespace aicad
