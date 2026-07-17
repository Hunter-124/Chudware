// core/agent/firm_team.cpp — see firm_team.h.
#include "agent/firm_team.h"

#include <algorithm>
#include <cctype>

#include "agent/firm_bid.h"

namespace aicad {
namespace agent {

namespace {

std::string lower(const std::string& s) {
    std::string o = s;
    std::transform(o.begin(), o.end(), o.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return o;
}

bool has_word(const std::string& hay, const std::string& w) {
    size_t p = hay.find(w);
    while (p != std::string::npos) {
        const bool lok = (p == 0) || !std::isalnum((unsigned char)hay[p - 1]);
        const size_t e = p + w.size();
        const bool rok = (e >= hay.size()) || !std::isalnum((unsigned char)hay[e]);
        if (lok && rok) return true;
        p = hay.find(w, p + 1);
    }
    return false;
}

}  // namespace

const std::vector<DisciplineRole>& discipline_roles() {
    static const std::vector<DisciplineRole> kRoles = {
        {Discipline::Mechanical, "Mechanical Design Engineer",
         "You are a senior mechanical design engineer. Design robust, manufacturable "
         "parametric solids: sound feature order, correct datums, wall thickness and "
         "draft for the process, standard hardware where it fits. Justify key dimensions."},
        {Discipline::Electrical, "Electrical/PCB Engineer",
         "You are a senior electrical engineer. Design correct schematics and PCBs: "
         "proper nets, decoupling, power integrity, DRC-clean layout, real footprints. "
         "Flag signal-integrity and thermal risks."},
        {Discipline::Structural, "Structural Engineer",
         "You are a structural engineer. Size load-bearing geometry to a factor of "
         "safety with named material properties; prefer FEA-verified sections over guesses."},
        {Discipline::Thermal, "Thermal Engineer",
         "You are a thermal engineer. Manage heat paths, cooling, and airflow; back "
         "choices with conduction/convection estimates and CFD where warranted."},
        {Discipline::Manufacturing, "Manufacturing/CAM Engineer",
         "You are a manufacturing engineer. Plan setups, stock, tools and toolpaths for "
         "an achievable, collision-free process within feeds/speeds; post to the controller."},
        {Discipline::Drafting, "Drafting/GD&T Engineer",
         "You are a drafting engineer. Produce complete, unambiguous drawings: correct "
         "views, fully-constrained dimensions, and ASME Y14.5 GD&T tied to real datums."},
        {Discipline::Sourcing, "Sourcing Engineer",
         "You are a sourcing engineer. Prefer real off-the-shelf catalog parts with "
         "correct dimensions; build an accurate, procurable BOM."},
        {Discipline::Generalist, "Project Engineer (Generalist)",
         "You are a project engineer coordinating a cross-discipline job. Decompose the "
         "work, delegate to the right specialist, and integrate their deliverables."},
    };
    return kRoles;
}

Discipline discipline_for_domain(Domain d) {
    switch (d) {
        case Domain::Modeling: return Discipline::Mechanical;
        case Domain::Sheetmetal: return Discipline::Mechanical;
        case Domain::Assembly: return Discipline::Mechanical;
        case Domain::Cam: return Discipline::Manufacturing;
        case Domain::Ecad: return Discipline::Electrical;
        case Domain::Drawing: return Discipline::Drafting;
        case Domain::Simulation: return Discipline::Structural;
        case Domain::Sourcing: return Discipline::Sourcing;
        case Domain::Research: return Discipline::Generalist;
    }
    return Discipline::Generalist;
}

const DisciplineRole& role_for_discipline(Discipline disc) {
    for (const auto& r : discipline_roles())
        if (r.discipline == disc) return r;
    return discipline_roles().back();  // Generalist
}

const DisciplineRole& role_for_domain(Domain d) {
    return role_for_discipline(discipline_for_domain(d));
}

std::vector<Domain> secondary_domains(const std::string& request, Domain primary) {
    const std::string r = lower(request);
    std::vector<Domain> out;
    auto add = [&](Domain d, bool hit) {
        if (hit && d != primary && std::find(out.begin(), out.end(), d) == out.end())
            out.push_back(d);
    };
    add(Domain::Ecad, has_word(r, "pcb") || has_word(r, "circuit") || has_word(r, "electronics") ||
                          has_word(r, "schematic") || has_word(r, "wiring"));
    add(Domain::Cam, has_word(r, "machined") || has_word(r, "milled") || has_word(r, "cnc") ||
                         has_word(r, "toolpath") || has_word(r, "manufacture"));
    add(Domain::Simulation, has_word(r, "fea") || has_word(r, "stress") || has_word(r, "thermal") ||
                                has_word(r, "cfd") || has_word(r, "simulate"));
    add(Domain::Drawing, has_word(r, "drawing") || has_word(r, "dimensioned") ||
                             has_word(r, "gd&t") || has_word(r, "tolerance"));
    add(Domain::Sourcing, has_word(r, "bom") || has_word(r, "off-the-shelf") ||
                              has_word(r, "sourced") || has_word(r, "purchase"));
    return out;
}

std::vector<TeamMember> assemble_team(const std::string& request, Domain primary) {
    std::vector<TeamMember> team;
    // Prime contractor: the cheapest in-scope specialist for the primary domain.
    auto firm_for = [](Domain d) -> Firm {
        for (const Firm& f : firm_roster())
            if (!f.generalist && f.specialty == d) return f;
        // fall back to the first generalist
        for (const Firm& f : firm_roster())
            if (f.generalist) return f;
        return firm_roster().front();
    };
    Firm lead = firm_for(primary);
    team.push_back({lead.id, lead.name, role_for_domain(primary).title, primary,
                    /*lead=*/true, /*subcontractor=*/false});
    // Subcontractors for each secondary discipline the request also needs.
    for (Domain sd : secondary_domains(request, primary)) {
        Firm sub = firm_for(sd);
        team.push_back({sub.id, sub.name, role_for_domain(sd).title, sd,
                        /*lead=*/false, /*subcontractor=*/true});
    }
    return team;
}

}  // namespace agent
}  // namespace aicad
