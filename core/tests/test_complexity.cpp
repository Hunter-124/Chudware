// Unit tests for agent/complexity.cpp (the deterministic task-complexity router).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/complexity.h"

using aicad::agent::classify_complexity;
using aicad::agent::Complexity;

TEST_CASE("canonical standoff is SOLO (skips planner)") {
    // THE user's example: dimension-clause commas must NOT count as object joiners.
    auto v = classify_complexity("a knurled hex standoff, 22mm tall, M3 clearance bore");
    CHECK(v.level == Complexity::Solo);
}

TEST_CASE("bare catalog parts are SOLO") {
    CHECK(classify_complexity("M3 x 10 socket head cap screw").level == Complexity::Solo);
    CHECK(classify_complexity("a 608 bearing").level == Complexity::Solo);
    CHECK(classify_complexity("an M5 washer").level == Complexity::Solo);
    CHECK(classify_complexity("a 20mm cube").level == Complexity::Solo);
}

TEST_CASE("assemblies and mechanisms are COMPLEX") {
    CHECK(classify_complexity("a two-stage planetary gearbox assembly").level ==
          Complexity::Complex);
    CHECK(classify_complexity("design a robot arm mechanism").level == Complexity::Complex);
    CHECK(classify_complexity("a quadcopter drone frame").level == Complexity::Complex);
    CHECK(classify_complexity("an enclosure with a hinged lid and latch").level ==
          Complexity::Complex);
}

TEST_CASE("joined multiple objects are STANDARD (not solo)") {
    // Two real objects joined by 'and' -> needs a plan, not a solo loop.
    auto v = classify_complexity("a mounting plate and a matching cover with vents");
    CHECK(v.level != Complexity::Solo);
}

TEST_CASE("a normal multi-feature part is STANDARD") {
    auto v = classify_complexity(
        "a control panel faceplate with a display cutout, four M3 mounting holes, "
        "chamfered edges and an engraved logo");
    CHECK(v.level == Complexity::Standard);
}

TEST_CASE("label strings are stable") {
    CHECK(std::string(classify_complexity("a nut").label()) == "solo");
    CHECK(std::string(classify_complexity("a gearbox assembly").label()) == "complex");
}
