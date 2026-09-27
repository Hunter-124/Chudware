// core/tests/test_parts.cpp — parametric standard-parts catalog (OCCT-free).
// Validates the catalog listing + that build() emits ops with the exact published
// dimensions. Geometry compilation is covered by the doc engine; here we assert the
// op JSON the generator hands to doc_apply_ops is well-formed and dimensionally right.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "parts/parts.h"
#include "parts/parts_data.h"

#include <algorithm>
#include <limits>

using namespace chudware::parts;

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

TEST_CASE("metric hex bolt envelopes use ISO 4017 head dimensions and under-head lengths") {
    // Independent nominal s/k values from Westfield's ISO 4017 table 1.
    // Exercise even the small sizes accepted by build() outside the UI size menu.
    const struct { const char* size; double d, af, head_h; } cases[] = {
        {"M1.6", 1.6, 3.2, 1.1}, {"M2", 2.0, 4.0, 1.4},
        {"M2.5", 2.5, 5.0, 1.7}, {"M3", 3.0, 5.5, 2.0},
        {"M4", 4.0, 7.0, 2.8}, {"M5", 5.0, 8.0, 3.5},
        {"M6", 6.0, 10.0, 4.0}, {"M8", 8.0, 13.0, 5.3},
        {"M10", 10.0, 16.0, 6.4}, {"M12", 12.0, 18.0, 7.5},
    };
    for (const auto& c : cases) {
        INFO("hex bolt " << c.size);
        const PartBuild pb = build("hex_bolt", c.size, 40.0, {{"standard", "ISO4017"}});
        const json* head = find_feat(pb.ops, "extrude");
        const json* shank = find_feat(pb.ops, "cylinder");
        REQUIRE(head);
        REQUIRE(shank);
        // Check the emitted solid dimensions, not just the catalog metadata.
        CHECK((*head)["params"]["depth"]["value"].get<double>() == doctest::Approx(c.head_h));
        CHECK(cyl_radius(*shank) == doctest::Approx(c.d / 2.0));
        CHECK_FALSE((*shank)["params"]["centered"].get<bool>());
        const double shank_z = (*shank)["params"]["at"][2].get<double>();
        const double shank_h = (*shank)["params"]["height"]["value"].get<double>();
        CHECK(shank_z == doctest::Approx(c.head_h));
        CHECK(shank_h == doctest::Approx(40.0));
        CHECK(shank_z + shank_h == doctest::Approx(c.head_h + 40.0));
        // The current hex orientation has vertical flats at x = +/- af/2.
        double xmin = std::numeric_limits<double>::infinity();
        double xmax = -std::numeric_limits<double>::infinity();
        for (const json& op : pb.ops) {
            if (op.value("op", "") != "sketch_add_entity") continue;
            for (const char* endpoint : {"p1", "p2"}) {
                const double x = op["entity"][endpoint][0].get<double>();
                xmin = std::min(xmin, x);
                xmax = std::max(xmax, x);
            }
        }
        CHECK(xmin == doctest::Approx(-c.af / 2.0));
        CHECK(xmax == doctest::Approx(c.af / 2.0));
    }
}

TEST_CASE("parts refuse unimplemented standard and property-class selections") {
    const PartBuild metric = build("hex_bolt", "M12", 40.0, {{"standard", "ISO 4017"}});
    const json* head = find_feat(metric.ops, "extrude");
    REQUIRE(head);
    CHECK((*head)["params"]["depth"]["value"].get<double>() == doctest::Approx(7.5));
    CHECK_THROWS_AS(build("hex_bolt", "M12", 40.0, {{"standard", "DIN 933"}}), std::invalid_argument);
    CHECK_THROWS_AS(build("hex_bolt", "1/4-20", 40.0, {{"standard", "ISO4017"}}), std::invalid_argument);
    CHECK_THROWS_AS(build("hex_bolt", "1/4-20", 40.0, {{"standard", "ASME B18.2.1"}}), std::invalid_argument);
    CHECK_THROWS_AS(build("shcs", "M12", 40.0, {{"standard", "ISO4017"}}), std::invalid_argument);
    CHECK_THROWS_AS(build("hex_bolt", "M12", 40.0,
                          {{"standard", "ISO4017"}, {"property_class", "10.9"}}), std::invalid_argument);
    CHECK_THROWS_AS(build("hex_bolt", "M12", 40.0, {{"property_class", nullptr}}), std::invalid_argument);
    CHECK_THROWS_AS(build("hex_bolt", "M12", 40.0, {{"standard", 4017}}), std::invalid_argument);
    CHECK_THROWS_AS(build("hex_bolt", "M12", 40.0, {{"finish", "zinc"}}), std::invalid_argument);
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

TEST_CASE("bearing 6205 — 25x52x15 ISO 15") {
    PartBuild pb = build("bearing", "6205", 0.0, json::object());
    const json *outer = nullptr, *bore = nullptr;
    for (const json& op : pb.ops) {
        const std::string nm = op["feature"].value("name", "");
        if (nm == "outer") outer = &op["feature"];
        if (nm == "bore") bore = &op["feature"];
    }
    REQUIRE(outer);
    REQUIRE(bore);
    CHECK(cyl_radius(*outer) == doctest::Approx(52.0 / 2.0));
    CHECK(cyl_radius(*bore) == doctest::Approx(25.0 / 2.0));
    CHECK((*outer)["params"]["height"]["value"].get<double>() == doctest::Approx(15.0));
    CHECK(pb.meta["specs"]["bore"].get<double>() == doctest::Approx(25.0));
    CHECK(pb.meta["specs"]["od"].get<double>() == doctest::Approx(52.0));
    CHECK(pb.meta["specs"]["width"].get<double>() == doctest::Approx(15.0));
}

TEST_CASE("every ISO 15 bearing size in the table builds") {
    // Must cover the bolts YAML advertised set (engine_oracle/data/bolts/bearings.blt.yaml).
    static const char* kIso15[] = {
        "623", "624", "625", "626", "608",
        "6000", "6001", "6002", "6003", "6004", "6005", "6006", "6007", "6008",
        "6200", "6201", "6202", "6203", "6204", "6205", "6206", "6207", "6208",
        "6300", "6301", "6302", "6303", "6304", "6305",
        "6800", "6801", "6802", "6900", "6901",
    };
    for (const char* size : kIso15) {
        INFO("bearing size " << size);
        const data::BearingRow* row = data::find_bearing(size);
        REQUIRE(row);
        PartBuild pb = build("bearing", size, 0.0, json::object());
        CHECK(pb.ops.size() >= 2);
        CHECK(pb.meta["specs"]["bore"].get<double>() == doctest::Approx(row->bore));
        CHECK(pb.meta["specs"]["od"].get<double>() == doctest::Approx(row->od));
        CHECK(pb.meta["specs"]["width"].get<double>() == doctest::Approx(row->width));
    }
}

TEST_CASE("unknown family / size throws") {
    CHECK_THROWS_AS(build("nonesuch", "M3", 10.0, json::object()), std::invalid_argument);
    CHECK_THROWS_AS(build("shcs", "M99", 10.0, json::object()), std::invalid_argument);
}

TEST_CASE("hobby servo MG90S has TowerPro envelope, usable mounting pattern and output datum") {
    // TowerPro A–F mechanical drawing: https://towerpro.com.tw/product/mg90s-3/
    const json cat = catalog_json();
    bool offered = false;
    for (const json& family : cat["families"])
        if (family["key"] == "hobby_servo")
            offered = std::find(family["sizes"].begin(), family["sizes"].end(), "MG90S")
                      != family["sizes"].end();
    REQUIRE(offered);
    const PartBuild pb = build("hobby_servo", "MG90S", 0, json::object());
    REQUIRE(pb.ops.size() == 6);
    const json& body = pb.ops[0]["feature"]["params"];
    const json& flange = pb.ops[1]["feature"]["params"];
    CHECK(body["length"]["value"].get<double>() == doctest::Approx(22.8));
    CHECK(body["width"]["value"].get<double>() == doctest::Approx(12.4));
    CHECK(body["height"]["value"].get<double>() == doctest::Approx(28.4));
    CHECK(body["at"][0].get<double>() == doctest::Approx(-11.4));
    CHECK(flange["length"]["value"].get<double>() == doctest::Approx(32.1));
    CHECK(flange["at"][0].get<double>() == doctest::Approx(-16.05));
    CHECK(flange["at"][2].get<double>() == doctest::Approx(18.5));
    CHECK(flange["height"]["value"].get<double>() == doctest::Approx(2.5));
    for (int i = 2; i <= 3; ++i) {
        const json& hole = pb.ops[i]["feature"]["params"];
        CHECK(hole["mode"] == "cut");
        CHECK(hole["radius"]["value"].get<double>() == doctest::Approx(1.1));
        CHECK(hole["at"][0].get<double>() == doctest::Approx(i == 2 ? -14.0 : 14.0));
    }
    const json& boss = pb.ops[4]["feature"]["params"];
    const json& spline = pb.ops[5]["feature"]["params"];
    CHECK(boss["at"][0].get<double>() == doctest::Approx(5.8));
    CHECK(boss["radius"]["value"].get<double>() == doctest::Approx(3.0));
    CHECK(boss["at"][2].get<double>() == doctest::Approx(28.4));
    CHECK(spline["at"][2].get<double>() + spline["height"]["value"].get<double>()
          == doctest::Approx(32.5));
    CHECK(pb.meta["output_axis"]["origin"][0].get<double>() == doctest::Approx(5.8));
    CHECK(pb.meta["output_axis"]["origin"][2].get<double>() == doctest::Approx(30.9));
    CHECK(pb.meta["specs"]["spline_teeth"] == 20);
    CHECK(pb.meta["specs"]["source"] == "https://towerpro.com.tw/product/mg90s-3/");
}

TEST_CASE("every advertised hobby servo size builds without assuming an interchangeable spline") {
    const json cat = catalog_json();
    for (const json& family : cat["families"]) {
        if (family["key"] != "hobby_servo") continue;
        for (const std::string size : family["sizes"].get<std::vector<std::string>>()) {
            INFO(size);
            const PartBuild pb = build("hobby_servo", size, 0, json::object());
            CHECK(pb.ops.size() == 6);
            CHECK(pb.meta["specs"]["source"].is_string());
            CHECK(pb.meta["output_axis"]["axis"] == json::array({0, 0, 1}));
        }
    }
    CHECK_THROWS_AS(build("hobby_servo", "UNKNOWN", 0, json::object()), std::invalid_argument);
}
