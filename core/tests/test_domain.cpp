// Unit tests for agent/domain.cpp (task-domain classifier + tool-class registry).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <string>

#include "agent/domain.h"

using chudware::agent::classify_domain;
using chudware::agent::context_sheets_for_domain;
using chudware::agent::Domain;
using chudware::agent::tool_classes_for_domain;

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

TEST_CASE("mech/aero rocket-plane is Modeling not ECAD") {
    // Multi-domain brief that names an MCU must not flip the whole request to ECAD —
    // weak electronics tokens lose to mechanical/aero design cues.
    const char* rocket =
        "Design a model rocket plane (boost-glide airframe) with good aerodynamics, "
        "tight budget, and manufacturability; body tube, fins, payload bay. Optional "
        "flight computer MCU later.";
    auto v = classify_domain(rocket);
    CHECK(v.domain == Domain::Modeling);
    CHECK(v.reason.find("electronics/PCB") == std::string::npos);
    // Reason should surface mechanical matches for the Routing card.
    CHECK(v.reason.find("rocket") != std::string::npos);
}

TEST_CASE("pure PCB still routes to ECAD") {
    CHECK(classify_domain("design a PCB for an ESP32 with a buck regulator").domain ==
          Domain::Ecad);
    CHECK(classify_domain("route the schematic netlist and export gerber").domain ==
          Domain::Ecad);
}

TEST_CASE("McMaster / parts-library is Sourcing capability") {
    auto v = classify_domain(
        "download a model from McMaster to test tooling for the parts library");
    CHECK(v.domain == Domain::Sourcing);
    CHECK(chudware::agent::is_capability_request(
        "download a model from McMaster to test tooling for the parts library"));
    CHECK_FALSE(chudware::agent::capability_request_reason(
                    "download a model from McMaster to test tooling for the parts library")
                    .empty());
}

TEST_CASE("capability helper accepts product surfaces, rejects garbage") {
    CHECK(chudware::agent::is_capability_request("run FEA stress analysis at 200N"));
    CHECK(chudware::agent::is_capability_request("generate milling toolpaths and post g-code"));
    CHECK(chudware::agent::is_capability_request("make an engineering drawing with a section view"));
    CHECK(chudware::agent::is_capability_request("reverse engineer this STEP into features"));
    CHECK(chudware::agent::is_capability_request("measure mass properties of the bracket"));
    CHECK(chudware::agent::is_capability_request(
        "initialize the built-in PLM/version-control system for this project"));
    CHECK(chudware::agent::is_capability_request(
        "check in a checkpoint and deprecate the old parts in the vault"));
    CHECK_FALSE(chudware::agent::is_capability_request("asdf qwerty blah blah"));
    CHECK_FALSE(chudware::agent::is_capability_request(""));
    CHECK_FALSE(chudware::agent::is_capability_request("hello how are you"));
}

// Regression: a project/file/PLM administration brief must be classified as a
// NON-GEOMETRY capability. Before this, the capability override accepted it and
// fed the modeling pipeline a synthetic spec, so every admin step in the plan came
// back as an extruded plate in a bogus part document.
TEST_CASE("admin/PLM cleanup brief is non-geometry, never modeling work") {
    const std::string brief =
        "I want you to look at all the documents in this folder/project and get them "
        "cleaned up & put into the plm system properly, and clean up the file system "
        "while your at it too. lets have it all well organized and have all the "
        "depricated stuff in specified \"depricated\" folders, move the unrelated work "
        "into /documents/AI-CAD, and keep the .step files seperate as well. you need to "
        "rename a lot of the parts too and just make the names simpler.";
    CHECK(chudware::agent::is_admin_request(brief));
    CHECK(chudware::agent::capability_kind(brief) ==
          chudware::agent::CapabilityKind::NonGeometry);
    CHECK(chudware::agent::is_capability_request(brief));
}

TEST_CASE("non-geometry capabilities are tagged as such") {
    using chudware::agent::capability_kind;
    using chudware::agent::CapabilityKind;
    CHECK(capability_kind("initialize the built-in PLM vault for this project") ==
          CapabilityKind::NonGeometry);
    CHECK(capability_kind("check in a checkpoint and deprecate the old parts") ==
          CapabilityKind::NonGeometry);
    CHECK(capability_kind("measure mass properties of the bracket") ==
          CapabilityKind::NonGeometry);
    CHECK(capability_kind("export the bill of materials") == CapabilityKind::NonGeometry);
}

TEST_CASE("geometry capabilities still route into the modeling pipeline") {
    using chudware::agent::capability_kind;
    using chudware::agent::CapabilityKind;
    CHECK(capability_kind("download a model from McMaster") == CapabilityKind::Geometry);
    CHECK(capability_kind("reverse engineer this STEP into features") ==
          CapabilityKind::Geometry);
    CHECK(capability_kind("generate milling toolpaths and post g-code") ==
          CapabilityKind::Geometry);
    CHECK(capability_kind("design a PCB for an ESP32") == CapabilityKind::Geometry);
}

// A real design brief that merely MENTIONS version control must still model.
TEST_CASE("design brief that mentions PLM is not demoted to admin") {
    const std::string brief =
        "design a mounting bracket with two M5 holes, then check it into PLM";
    CHECK_FALSE(chudware::agent::is_admin_request(brief));
    CHECK(classify_domain(brief).domain == Domain::Modeling);
}
