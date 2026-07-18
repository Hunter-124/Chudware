// Unit tests for agent/domain.cpp (task-domain classifier + tool-class registry).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>

#include "agent/domain.h"

using aicad::agent::classify_domain;
using aicad::agent::context_sheets_for_domain;
using aicad::agent::Domain;
using aicad::agent::tool_classes_for_domain;

static bool has(const std::vector<std::string>& v, const std::string& s) {
    return std::find(v.begin(), v.end(), s) != v.end();
}

TEST_CASE("modeling is the default domain") {
    CHECK(classify_domain("a knurled hex standoff, 22mm tall, M3 clearance bore").domain ==
          Domain::Modeling);
    CHECK(classify_domain("a bracket with two mounting holes").domain == Domain::Modeling);
}

TEST_CASE("electronics routes to ECAD") {
    CHECK(classify_domain("design a PCB for an ESP32 with a buck regulator").domain ==
          Domain::Ecad);
    CHECK(classify_domain("draw the schematic and route the traces").domain == Domain::Ecad);
}

TEST_CASE("machining routes to CAM") {
    CHECK(classify_domain("generate the milling toolpaths and post g-code").domain ==
          Domain::Cam);
}

TEST_CASE("analysis routes to simulation") {
    CHECK(classify_domain("run a static stress FEA at 200N").domain == Domain::Simulation);
    CHECK(classify_domain("what is the factor of safety under load").domain ==
          Domain::Simulation);
}

TEST_CASE("drawings and assemblies route correctly") {
    CHECK(classify_domain("make an engineering drawing with a section view").domain ==
          Domain::Drawing);
    CHECK(classify_domain("assemble the gearbox and add mates").domain == Domain::Assembly);
}

TEST_CASE("sheetmetal enclosure routes to Sheetmetal") {
    // Battery corpus: sm_* ops domain. Explicit "sheetmetal" wins over Modeling default.
    CHECK(classify_domain("sheetmetal enclosure 200x120x60mm, 1.5mm steel").domain ==
          Domain::Sheetmetal);
    CHECK(classify_domain("a sheet metal bracket with a hem").domain == Domain::Sheetmetal);
    // tool classes include the sheetmetal family so host can grant sm_* ops.
    auto t = tool_classes_for_domain(Domain::Sheetmetal);
    CHECK(has(t, "sheetmetal"));
    CHECK(has(t, "modeling"));
    CHECK(has(context_sheets_for_domain(Domain::Sheetmetal), "sheetmetal"));
}

TEST_CASE("bolt-together multi-part routes to Assembly") {
    CHECK(classify_domain("two brackets that bolt together with M5 assembly").domain ==
          Domain::Assembly);
    CHECK(classify_domain("plates that screw together with M3 hardware").domain ==
          Domain::Assembly);
}

TEST_CASE("tool-class scoping is domain-narrow (context economy)") {
    // A modeling task must NOT carry CAM/ECAD tool classes.
    auto m = tool_classes_for_domain(Domain::Modeling);
    CHECK(has(m, "modeling"));
    CHECK(has(m, "read"));
    CHECK_FALSE(has(m, "cam"));
    CHECK_FALSE(has(m, "ecad"));
    CHECK_FALSE(has(m, "simulation"));

    // An ECAD task carries ecad, not modeling/cam.
    auto e = tool_classes_for_domain(Domain::Ecad);
    CHECK(has(e, "ecad"));
    CHECK_FALSE(has(e, "modeling"));
    CHECK_FALSE(has(e, "cam"));

    // Every domain keeps the safe read core.
    CHECK(has(tool_classes_for_domain(Domain::Cam), "read"));
    CHECK(has(tool_classes_for_domain(Domain::Simulation), "read"));
}

TEST_CASE("context sheets are on-topic") {
    auto m = context_sheets_for_domain(Domain::Modeling);
    CHECK(has(m, "extrude"));
    CHECK(has(m, "hardware"));
    // ECAD context is just the ecad sheet, not the modeling primitives.
    auto e = context_sheets_for_domain(Domain::Ecad);
    CHECK(has(e, "ecad"));
    CHECK_FALSE(has(e, "extrude"));
}

TEST_CASE("labels are stable") {
    CHECK(std::string(classify_domain("a PCB").label()) == "ecad");
    CHECK(std::string(classify_domain("a cube").label()) == "modeling");
}
