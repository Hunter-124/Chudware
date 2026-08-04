// Unit tests for agent/firm_bid.cpp (deterministic token estimator + firm bids).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/firm_bid.h"

using namespace chudware::agent;

TEST_CASE("token estimate scales with complexity") {
    auto solo = estimate_task_tokens("a nut", Complexity::Solo, Domain::Modeling);
    auto std_ = estimate_task_tokens("a nut", Complexity::Standard, Domain::Modeling);
    auto cplx = estimate_task_tokens("a nut", Complexity::Complex, Domain::Modeling);
    CHECK(solo.total_tokens < std_.total_tokens);
    CHECK(std_.total_tokens < cplx.total_tokens);
    CHECK(solo.total_tokens == solo.input_tokens + solo.output_tokens);
}

TEST_CASE("estimate is deterministic") {
    auto a = estimate_task_tokens("a bracket with holes", Complexity::Standard, Domain::Modeling);
    auto b = estimate_task_tokens("a bracket with holes", Complexity::Standard, Domain::Modeling);
    CHECK(a.total_tokens == b.total_tokens);
}


TEST_CASE("specialist in-domain underbids generalists") {
    // For an ECAD task, the Circuit Labs specialist should bid fewer tokens than the
    // generalist firms (which pay a spread-thin penalty off their native domain).
    auto bids =
        collect_bids("route a PCB for an ESP32", Complexity::Standard, Domain::Ecad);
    REQUIRE(!bids.empty());
    // Cheapest in-scope bid should be the ECAD specialist.
    CHECK(bids.front().in_scope);
    CHECK(bids.front().firm_id == "circuit_labs");
    long specialist = -1, generalist = -1;
    for (const auto& b : bids) {
        if (b.firm_id == "circuit_labs") specialist = b.tokens;
        if (b.firm_id == "gen_engineering") generalist = b.tokens;
    }
    REQUIRE(specialist > 0);
    REQUIRE(generalist > 0);
    CHECK(specialist < generalist);
}


TEST_CASE("roster covers every domain with a specialist") {
    const auto& roster = firm_roster();
    for (Domain d : {Domain::Modeling, Domain::Sheetmetal, Domain::Assembly, Domain::Cam,
                     Domain::Ecad, Domain::Drawing, Domain::Simulation, Domain::Sourcing}) {
        bool covered = false;
        for (const auto& f : roster)
            if (!f.generalist && f.specialty == d) { covered = true; break; }
        CHECK(covered);
    }
}
