// core/agent/firm_team.h — engineering-firm team assembly + specialist role prompts.
//
// The Eng. Firm org, once a lead firm wins the bid (firm_bid.h), assembles a TEAM:
// a lead (prime contractor) specialist in the task's primary domain, plus
// SUBCONTRACTORS hired for any secondary disciplines the request also touches
// (a modeled enclosure that also needs a PCB -> subcontract the ECAD firm). Each
// member carries a discipline ROLE with a specialist system-prompt seed so the
// dispatched agent starts with the right expert framing.
//
// Deterministic (string heuristics over the domain classifier); no LLM, no I/O.
#pragma once

#include <string>
#include <vector>

#include "agent/domain.h"

namespace aicad {
namespace agent {

enum class Discipline {
    Mechanical,     // solid modeling, mechanisms, DFM
    Electrical,     // schematics, PCB, power
    Structural,     // load-bearing frames, FEA-driven sizing
    Thermal,        // heat, cooling, CFD
    Manufacturing,  // CAM, toolpaths, process planning
    Drafting,       // 2D drawings, GD&T
    Sourcing,       // standard parts, BOM, procurement
    Generalist,     // cross-discipline coordinator / fallback
};

struct DisciplineRole {
    Discipline discipline;
    std::string title;        // e.g. "Mechanical Design Engineer"
    std::string system_seed;  // specialist starting-prompt seed for the dispatched agent
};

// The full specialist + generalist role roster.
const std::vector<DisciplineRole>& discipline_roles();

// Map a task domain to its lead discipline + role.
Discipline discipline_for_domain(Domain d);
const DisciplineRole& role_for_discipline(Discipline disc);
const DisciplineRole& role_for_domain(Domain d);

struct TeamMember {
    std::string firm_id;
    std::string firm_name;
    std::string role_title;    // the discipline role this member fills
    Domain scope;              // the domain this member covers
    bool lead;                 // prime contractor (owns the deliverable)
    bool subcontractor;        // hired for a secondary cross-discipline need
};

// Secondary domains the request references beyond `primary` (drives subcontracting).
std::vector<Domain> secondary_domains(const std::string& request, Domain primary);

// Assemble a team for a task: LEAD specialist in `primary` + a SUBCONTRACTOR per
// secondary domain the request also needs. Deterministic + order-stable.
std::vector<TeamMember> assemble_team(const std::string& request, Domain primary);

}  // namespace agent
}  // namespace aicad
