// Unit tests for agent/firm_team.cpp (team assembly + specialist roles).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>

#include "agent/firm_team.h"

using namespace aicad::agent;

static bool hasFirm(const std::vector<TeamMember>& t, const std::string& id) {
    return std::any_of(t.begin(), t.end(), [&](const TeamMember& m) { return m.firm_id == id; });
}

TEST_CASE("every domain maps to a specialist role with a prompt seed") {
    for (Domain d : {Domain::Modeling, Domain::Cam, Domain::Ecad, Domain::Drawing,
                     Domain::Simulation, Domain::Sourcing}) {
        const DisciplineRole& r = role_for_domain(d);
        CHECK_FALSE(r.title.empty());
        CHECK(r.system_seed.size() > 20);  // a real specialist seed, not a stub
    }
}

TEST_CASE("domain -> discipline mapping is sensible") {
    CHECK(discipline_for_domain(Domain::Ecad) == Discipline::Electrical);
    CHECK(discipline_for_domain(Domain::Cam) == Discipline::Manufacturing);
    CHECK(discipline_for_domain(Domain::Modeling) == Discipline::Mechanical);
    CHECK(discipline_for_domain(Domain::Drawing) == Discipline::Drafting);
}

TEST_CASE("a single-domain job is a lone lead, no subcontractors") {
    auto team = assemble_team("a knurled hex standoff, 22mm, M3 bore", Domain::Modeling);
    REQUIRE(team.size() == 1);
    CHECK(team[0].lead);
    CHECK_FALSE(team[0].subcontractor);
    CHECK(team[0].scope == Domain::Modeling);
}

TEST_CASE("cross-discipline job subcontracts the secondary specialists") {
    // A modeled enclosure that ALSO needs a PCB and a drawing -> lead mech + ECAD + drafting subs.
    auto team = assemble_team(
        "an enclosure with a PCB inside and a dimensioned drawing for manufacture",
        Domain::Modeling);
    CHECK(team.size() >= 3);
    CHECK(team[0].lead);
    CHECK(team[0].scope == Domain::Modeling);
    // secondary domains detected -> subcontractors present
    bool hasEcadSub = false, hasCamSub = false, hasDrawSub = false;
    for (const auto& m : team) {
        if (m.subcontractor && m.scope == Domain::Ecad) hasEcadSub = true;
        if (m.subcontractor && m.scope == Domain::Cam) hasCamSub = true;
        if (m.subcontractor && m.scope == Domain::Drawing) hasDrawSub = true;
    }
    CHECK(hasEcadSub);
    CHECK(hasCamSub);
    CHECK(hasDrawSub);
    // the lead is the mechanical specialist firm
    CHECK(hasFirm(team, "mech_precision"));
}

TEST_CASE("secondary_domains excludes the primary") {
    auto sec = secondary_domains("a machined bracket with a wiring harness", Domain::Modeling);
    CHECK(std::find(sec.begin(), sec.end(), Domain::Modeling) == sec.end());
    CHECK(std::find(sec.begin(), sec.end(), Domain::Cam) != sec.end());
    CHECK(std::find(sec.begin(), sec.end(), Domain::Ecad) != sec.end());
}

TEST_CASE("assembly is deterministic + order-stable") {
    auto a = assemble_team("enclosure with a PCB", Domain::Modeling);
    auto b = assemble_team("enclosure with a PCB", Domain::Modeling);
    REQUIRE(a.size() == b.size());
    for (size_t i = 0; i < a.size(); ++i) CHECK(a[i].firm_id == b[i].firm_id);
}
