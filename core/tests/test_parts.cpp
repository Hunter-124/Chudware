// core/tests/test_parts.cpp — parametric standard-parts catalog (OCCT-free).
// Validates the catalog listing + that build() emits ops with the exact published
// dimensions. Geometry compilation is covered by the doc engine; here we assert the
// op JSON the generator hands to doc_apply_ops is well-formed and dimensionally right.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "parts/parts.h"
#include "parts/parts_data.h"

using namespace aicad::parts;

// Find the first add_feature op of a given primitive/feature type.
static const json* find_feat(const json& ops, const std::string& type) {
    for (const json& op : ops) {
        if (op.value("op", "") == "add_feature" &&
            op["feature"].value("type", "") == type)
            return &op["feature"];
    }
    return nullptr;
}

// The radius value of a cylinder feature (dim object -> value).
static double cyl_radius(const json& feat) {
    return feat["params"]["radius"]["value"].get<double>();
}

TEST_CASE("catalog lists families with sizes") {
    json cat = catalog_json();
    CHECK(cat.contains("families"));
    CHECK(cat["families"].size() >= 12);
    bool saw_shcs = false, saw_bearing = false;
    for (const json& f : cat["families"]) {
        CHECK(f.contains("key"));
        CHECK(f.contains("sizes"));
        CHECK(!f["sizes"].empty());
        if (f["key"] == "shcs") {
            saw_shcs = true;
            // metric + SAE both present
            CHECK(f["needs_length"].get<bool>());
        }
        if (f["key"] == "bearing") saw_bearing = true;
    }
    CHECK(saw_shcs);
    CHECK(saw_bearing);
}

TEST_CASE("SHCS metric M3x10 — shank + head at exact ISO 4762 dims") {
    PartBuild pb = build("shcs", "M3", 10.0, json::object());
    CHECK(pb.ops.size() == 2);
    const json* shank = nullptr;
    const json* head = nullptr;
    for (const json& op : pb.ops) {
        const std::string nm = op["feature"].value("name", "");
        if (nm == "shank") shank = &op["feature"];
        if (nm == "head") head = &op["feature"];
    }
    REQUIRE(shank);
    REQUIRE(head);
    CHECK(cyl_radius(*shank) == doctest::Approx(1.5));     // M3 thread Ø 3.0 -> r 1.5
    CHECK(cyl_radius(*head) == doctest::Approx(2.75));     // dk 5.5 -> r 2.75
    CHECK((*head)["params"]["height"]["value"].get<double>() == doctest::Approx(3.0));  // k
    // head sits on top of the 10 mm shank
    CHECK((*head)["params"]["at"][2].get<double>() == doctest::Approx(10.0));
}

TEST_CASE("SHCS imperial 1/4-20 resolves SAE table") {
    PartBuild pb = build("shcs", "1/4-20", 20.0, json::object());
    CHECK(pb.ops.size() == 2);
    const json* shank = find_feat(pb.ops, "cylinder");
    REQUIRE(shank);
    CHECK(cyl_radius(*shank) == doctest::Approx(6.35 / 2.0));
}

TEST_CASE("hex nut M5 — hex prism + central bore") {
    PartBuild pb = build("hex_nut", "M5", 0.0, json::object());
    // sketch + 6 lines + extrude + bore cut
    int lines = 0, sketches = 0, extrudes = 0, cuts = 0;
    for (const json& op : pb.ops) {
        if (op.value("op", "") == "sketch_add_entity") lines++;
        if (op.value("op", "") == "add_feature") {
            const std::string t = op["feature"].value("type", "");
            if (t == "sketch") sketches++;
            if (t == "extrude") extrudes++;
            if (t == "cylinder" && op["feature"]["params"].value("mode", "") == "cut") cuts++;
        }
    }
    CHECK(sketches == 1);
    CHECK(lines == 6);
    CHECK(extrudes == 1);
    CHECK(cuts == 1);
    // bore radius = M5 nominal / 2
    const json* bore = nullptr;
    for (const json& op : pb.ops)
        if (op.value("op", "") == "add_feature" && op["feature"].value("name", "") == "bore")
            bore = &op["feature"];
    REQUIRE(bore);
    CHECK(cyl_radius(*bore) == doctest::Approx(2.5));
    // extrude depth = nut thickness 4.0 (ISO 4032 M5)
    const json* ex = find_feat(pb.ops, "extrude");
    REQUIRE(ex);
    CHECK((*ex)["params"]["depth"]["value"].get<double>() == doctest::Approx(4.0));
}

TEST_CASE("flat washer M4 — ring OD/ID per ISO 7089") {
    PartBuild pb = build("washer_flat", "M4", 0.0, json::object());
    const json *ring = nullptr, *bore = nullptr;
    for (const json& op : pb.ops) {
        const std::string nm = op["feature"].value("name", "");
        if (nm == "ring") ring = &op["feature"];
        if (nm == "bore") bore = &op["feature"];
    }
    REQUIRE(ring);
    REQUIRE(bore);
    CHECK(cyl_radius(*ring) == doctest::Approx(9.0 / 2.0));   // OD 9.0
    CHECK(cyl_radius(*bore) == doctest::Approx(4.3 / 2.0));   // ID 4.3
}

TEST_CASE("flat-head cap screw uses a cone head") {
    PartBuild pb = build("fhcs", "M4", 12.0, json::object());
    const json* cone = find_feat(pb.ops, "cone");
    REQUIRE(cone);
    // base radius = shank, top radius = flat head Ø / 2 (8.4/2)
    CHECK((*cone)["params"]["r1"]["value"].get<double>() == doctest::Approx(2.0));
    CHECK((*cone)["params"]["r2"]["value"].get<double>() == doctest::Approx(4.2));
}

TEST_CASE("bearing 608 — outer ring 22, bore 8, width 7") {
    PartBuild pb = build("bearing", "608", 0.0, json::object());
    const json *outer = nullptr, *bore = nullptr;
    for (const json& op : pb.ops) {
        const std::string nm = op["feature"].value("name", "");
        if (nm == "outer") outer = &op["feature"];
        if (nm == "bore") bore = &op["feature"];
    }
    REQUIRE(outer);
    REQUIRE(bore);
    CHECK(cyl_radius(*outer) == doctest::Approx(11.0));
    CHECK(cyl_radius(*bore) == doctest::Approx(4.0));
    CHECK((*outer)["params"]["height"]["value"].get<double>() == doctest::Approx(7.0));
}

TEST_CASE("unknown family / size throws") {
    CHECK_THROWS_AS(build("nonesuch", "M3", 10.0, json::object()), std::invalid_argument);
    CHECK_THROWS_AS(build("shcs", "M99", 10.0, json::object()), std::invalid_argument);
}
