// Unit tests for agent/complexity.cpp (the deterministic task-complexity router).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/complexity.h"

using aicad::agent::classify_complexity;
using aicad::agent::Complexity;
using aicad::agent::ExecStructure;
using aicad::agent::recommend_structure;
using aicad::agent::structure_label;

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

TEST_CASE("a single primitive with two dimensions stays SOLO (dimensional 'and')") {
    // The ' and ' chains a DIMENSION ("...diameter and 30mm tall"), not a second object, so a
    // plain primitive must not be over-planned. (Regression: these routed STANDARD before.)
    CHECK(classify_complexity("a cylinder 20mm in diameter and 30mm tall").level == Complexity::Solo);
    CHECK(classify_complexity("a hexagonal prism 16mm across flats and 20mm tall").level ==
          Complexity::Solo);
    CHECK(classify_complexity("a cone 30mm base and 50mm tall").level == Complexity::Solo);
    // a REAL second object still forces a plan
    CHECK(classify_complexity("a plate 50mm wide and a matching cover").level != Complexity::Solo);
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

TEST_CASE("structure recommendation tracks complexity") {
    // Solo part -> Solo structure
    auto solo = classify_complexity("a nut");
    CHECK(solo.level == Complexity::Solo);
    CHECK(recommend_structure(solo.level) == ExecStructure::Solo);
    CHECK(std::string(structure_label(recommend_structure(solo.level))) == "solo");

    // Standard multi-feature part -> Subagent
    auto standard = classify_complexity(
        "a control panel faceplate with a display cutout, four M3 mounting holes, "
        "chamfered edges and an engraved logo");
    CHECK(standard.level == Complexity::Standard);
    CHECK(recommend_structure(standard.level) == ExecStructure::Subagent);
    CHECK(std::string(structure_label(recommend_structure(standard.level))) == "subagent");

    // Complex assembly -> Dag by default, Company when cross-discipline
    auto complex = classify_complexity("a two-stage planetary gearbox assembly");
    CHECK(complex.level == Complexity::Complex);
    CHECK(recommend_structure(complex.level, /*cross_discipline=*/false) == ExecStructure::Dag);
    CHECK(std::string(structure_label(
              recommend_structure(complex.level, /*cross_discipline=*/false))) == "dag");
    CHECK(recommend_structure(complex.level, /*cross_discipline=*/true) == ExecStructure::Company);
    CHECK(std::string(structure_label(
              recommend_structure(complex.level, /*cross_discipline=*/true))) == "company");
}
