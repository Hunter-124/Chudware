// Unit tests for agent/complexity.cpp (the deterministic task-complexity router)
// and the domain casual-mention guard used alongside it.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/complexity.h"
#include "agent/domain.h"

using aicad::agent::classify_complexity;
using aicad::agent::classify_domain;
using aicad::agent::Complexity;
using aicad::agent::Domain;
using aicad::agent::ExecStructure;
using aicad::agent::recommend_structure;
using aicad::agent::structure_label;

TEST_CASE("canonical standoff is SOLO (skips planner)") {
    // THE user's example: dimension-clause commas must NOT count as object joiners.
    auto v = classify_complexity("a knurled hex standoff, 22mm tall, M3 clearance bore");
    CHECK(v.level == Complexity::Solo);
    CHECK(classify_domain("a knurled hex standoff, 22mm tall, M3 clearance bore").domain ==
          Domain::Modeling);
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

// ---- wave hardening -------------------------------------------------------

TEST_CASE("coordinate system and state machine are not COMPLEX false positives") {
    // Bare substring "system"/"machine" used to force Complex; word-boundary + bigram
    // exclusions keep these modeling/software phrases off the assembly path.
    CHECK(classify_complexity("align the part to the coordinate system origin").level !=
          Complexity::Complex);
    CHECK(classify_complexity("a bracket for the state machine enclosure panel").level !=
          Complexity::Complex);
    // Real multi-part "system" / "machine" still hit Complex.
    CHECK(classify_complexity("a cooling system with pump and reservoir").level ==
          Complexity::Complex);
    CHECK(classify_complexity("design a packaging machine").level == Complexity::Complex);
}

TEST_CASE("multi-part intent phrasing is COMPLEX") {
    CHECK(classify_complexity("two brackets that bolt together").level == Complexity::Complex);
    CHECK(classify_complexity("plates that screw together with M3 hardware").level ==
          Complexity::Complex);
    CHECK(classify_complexity("a clevis that mounts to the frame rail").level == Complexity::Complex);
    CHECK(classify_complexity("a cover that attaches to the housing").level == Complexity::Complex);
}

TEST_CASE("solo noun expansions stay SOLO") {
    // kSoloNouns additions: bushing, knob, bracket, cap, plug, flange (+ existing standoff/spacer).
    CHECK(classify_complexity("an M8 bronze bushing").level == Complexity::Solo);
    CHECK(classify_complexity("a knurled thumb knob").level == Complexity::Solo);
    CHECK(classify_complexity("a simple L bracket").level == Complexity::Solo);
    CHECK(classify_complexity("an end cap 20mm diameter").level == Complexity::Solo);
    CHECK(classify_complexity("a rubber plug 12mm").level == Complexity::Solo);
    CHECK(classify_complexity("a pipe flange 50mm").level == Complexity::Solo);
    CHECK(classify_complexity("a hex standoff 16mm AF").level == Complexity::Solo);
}

TEST_CASE("bare stress or load stays Modeling; analyze intent routes Simulation") {
    // Casual strength talk must not drag a modeling request into the FEA toolset.
    CHECK(classify_domain("a bracket that can handle stress").domain == Domain::Modeling);
    CHECK(classify_domain("a plate under heavy load").domain == Domain::Modeling);
    // Paired with analyze/simulate/fea words (or other sim-core terms) still routes Sim.
    CHECK(classify_domain("run a static stress FEA at 200N").domain == Domain::Simulation);
    CHECK(classify_domain("analyze the stress on this arm").domain == Domain::Simulation);
    CHECK(classify_domain("what is the factor of safety under load").domain == Domain::Simulation);
}

// ---- grok-4.5 battery round-1 corpus (locked) ------------------------------
// Every row asserts BOTH complexity level AND domain. Keep in lockstep with
// the battery table; classifier changes must keep this matrix green.

TEST_CASE("battery corpus: knurled hex standoff is Solo/Modeling") {
    const char* p = "a knurled hex standoff, 22mm tall, M3 clearance bore";
    CHECK(classify_complexity(p).level == Complexity::Solo);
    CHECK(classify_domain(p).domain == Domain::Modeling);
}

TEST_CASE("battery corpus: M6 washer is Solo/Modeling") {
    const char* p = "M6 washer, 1.5mm thick";
    CHECK(classify_complexity(p).level == Complexity::Solo);
    CHECK(classify_domain(p).domain == Domain::Modeling);
}

TEST_CASE("battery corpus: chamfer edit follow-up is Solo/Modeling") {
    const char* p = "chamfer the top edges (edit follow-up, existing doc)";
    CHECK(classify_complexity(p).level == Complexity::Solo);
    CHECK(classify_domain(p).domain == Domain::Modeling);
}

TEST_CASE("battery corpus: mounting plate with holes and ribs is Standard/Modeling") {
    // Single body, multi-feature feature-list ('and' joins ribs to holes, not a 2nd part).
    const char* p = "mounting plate with 4 clearance holes and 2 ribs";
    CHECK(classify_complexity(p).level == Complexity::Standard);
    CHECK(classify_domain(p).domain == Domain::Modeling);
}

TEST_CASE("battery corpus: hinge bracket is Standard-or-Complex/Modeling") {
    // Multi-part intent (two leaves + pin) may escalate to Complex; Standard is also
    // correct for a single hinge body. Document current: Standard (feature list).
    const char* p = "hinge bracket with two leaves and a pin";
    auto cx = classify_complexity(p);
    CHECK((cx.level == Complexity::Standard || cx.level == Complexity::Complex));
    CHECK(classify_domain(p).domain == Domain::Modeling);
}

TEST_CASE("battery corpus: two brackets bolt-together is Complex/Assembly") {
    const char* p = "two brackets that bolt together with M5 assembly";
    CHECK(classify_complexity(p).level == Complexity::Complex);
    CHECK(classify_domain(p).domain == Domain::Assembly);
}

TEST_CASE("battery corpus: sheetmetal enclosure is Standard/Sheetmetal") {
    const char* p = "sheetmetal enclosure 200x120x60mm, 1.5mm steel";
    CHECK(classify_complexity(p).level == Complexity::Standard);
    CHECK(classify_domain(p).domain == Domain::Sheetmetal);
}

TEST_CASE("battery corpus: coordinate system is Solo/Modeling (not Complex)") {
    // Battery-verified edge: "system" must not inflate via the assembly keyword.
    const char* p = "a coordinate system at the centroid";
    CHECK(classify_complexity(p).level == Complexity::Solo);
    CHECK(classify_domain(p).domain == Domain::Modeling);
}

TEST_CASE("battery corpus: knurled knob is Solo/Modeling (pattern must not inflate)") {
    const char* p = "knob with knurling pattern";
    CHECK(classify_complexity(p).level == Complexity::Solo);
    CHECK(classify_domain(p).domain == Domain::Modeling);
}

TEST_CASE("battery corpus: garbage prompt is Solo/Modeling (intake rejects, not classifier)") {
    const char* p = "asdf qwerty blah blah";
    // Must not crash; mechanically Solo/Modeling is fine. Rejection is intake's job.
    auto cx = classify_complexity(p);
    auto dm = classify_domain(p);
    CHECK(cx.level == Complexity::Solo);
    CHECK(dm.domain == Domain::Modeling);
    CHECK(cx.label() != nullptr);
    CHECK(dm.label() != nullptr);
}
