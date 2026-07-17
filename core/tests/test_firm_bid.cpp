// Unit tests for agent/firm_bid.cpp (deterministic token estimator + firm bids).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/firm_bid.h"

using namespace aicad::agent;

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

TEST_CASE("tokens_to_usd is linear and clamps negatives") {
    CHECK(tokens_to_usd(1000, 3.0) == doctest::Approx(3.0));
    CHECK(tokens_to_usd(500, 2.0) == doctest::Approx(1.0));
    CHECK(tokens_to_usd(1000, -1.0) == doctest::Approx(0.0));
}

TEST_CASE("specialist in-domain underbids generalists") {
    // For an ECAD task, the Circuit Labs specialist should bid fewer tokens than the
    // generalist firms (which pay a spread-thin penalty off their native domain).
    auto bids = collect_bids("route a PCB for an ESP32", Complexity::Standard, Domain::Ecad,
                             /*usd_per_1k=*/3.0, /*budget_usd=*/1000.0);
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

TEST_CASE("budget ceiling flags over-budget bids") {
    auto bids = collect_bids("a two-stage planetary gearbox assembly", Complexity::Complex,
                             Domain::Assembly, /*usd_per_1k=*/3.0, /*budget_usd=*/0.01);
    // A $0.01 ceiling can't cover a complex job -> every bid is over budget.
    for (const auto& b : bids) CHECK_FALSE(b.within_budget);

    // Unlimited budget (0) -> all in-scope bids are within budget.
    auto unl = collect_bids("a two-stage planetary gearbox assembly", Complexity::Complex,
                            Domain::Assembly, 3.0, /*budget_usd=*/0.0);
    for (const auto& b : unl) CHECK(b.within_budget);
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
