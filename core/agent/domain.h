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

namespace chudware {
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

// True when the request maps to a real product capability the app supports
// (parts-library / McMaster-style fetch, measure/evaluate, CAM, simulation,
// ECAD, reverse-engineering, drawing, sourcing) — even if it is not a greenfield
// solid-model design. Intake MUST accept these; only pure gibberish / empty /
// nothing-to-modify rejects stay terminal.
bool is_capability_request(const std::string& request);

// Short reason string for a capability hit (empty when is_capability_request is false).
std::string capability_request_reason(const std::string& request);

// What KIND of capability a request maps to. The distinction matters because the
// run pipeline is a modeling pipeline: accepting a capability request is only safe
// when that capability actually produces geometry. Non-geometry work (PLM /
// version-control, file + project organization, measure-only, BOM/sourcing) must
// terminate the run with a handoff instead of being fed a synthetic spec — else the
// planner turns every admin step into an extruded plate.
enum class CapabilityKind {
    None,         // not an in-product capability request
    Geometry,     // produces/edits geometry — safe to run the modeling pipeline
    NonGeometry,  // admin/analysis surface — must NOT enter the modeling pipeline
};

CapabilityKind capability_kind(const std::string& request);

// True when the request is dominated by project/file/PLM administration cues
// (organize folders, move files, deprecate, rename parts, check into version
// control) rather than by build-a-part cues. Deterministic, no LLM.
bool is_admin_request(const std::string& request);

// The SCOPED tool/capability classes for a domain — coarse capability tags a host
// maps to a concrete tool allowlist (keeps a modeling agent from loading CAM/ECAD
// tools). Always includes the domain's own class plus universally-safe read tags.
std::vector<std::string> tool_classes_for_domain(Domain d);

// The context cheatsheet names (shared/cheatsheets/<name>.md) relevant to a domain,
// so only on-topic reference material is injected. Empty = the role's default.
std::vector<std::string> context_sheets_for_domain(Domain d);

}  // namespace agent
}  // namespace chudware
