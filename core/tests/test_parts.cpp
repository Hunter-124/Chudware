// core/tests/test_parts.cpp — parametric standard-parts catalog (OCCT-free).
// Validates the catalog listing + that build() emits ops with the exact published
// dimensions. Geometry compilation is covered by the doc engine; here we assert the
// op JSON the generator hands to doc_apply_ops is well-formed and dimensionally right.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "parts/parts.h"
#include "parts/parts_data.h"
#include "parts/place.h"

#include <algorithm>
#include <cmath>
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

TEST_CASE("AS568 O-rings expose the complete table and a dimensioned revolve") {
    const auto& rows = data::orings_as568();
    REQUIRE(rows.size() == 336);
    const auto families = catalog_json()["families"];
    const auto family = std::find_if(families.begin(), families.end(),
        [](const json& f) { return f["key"] == "oring_as568"; });
    REQUIRE(family != families.end());
    CHECK((*family)["standard"] == "SAE AS568B");
    CHECK_FALSE((*family)["needs_length"].get<bool>());
    REQUIRE((*family)["sizes"].size() == rows.size());
    REQUIRE((*family)["size_sources"].size() == rows.size());

    for (std::size_t i = 0; i < rows.size(); ++i) {
        const auto& row = rows[i];
        INFO(row.size);
        CHECK((*family)["sizes"][i] == row.size);
        const auto& source = (*family)["size_sources"][row.size];
        CHECK(source["id_in"].get<double>() == doctest::Approx(row.id_mils / 1000.0));
        CHECK(source["cs_in"].get<double>() == doctest::Approx(row.cs_mils / 1000.0));
        CHECK(source["source"].get<std::string>().find("AS568B") != std::string::npos);

        const PartBuild pb = build("oring_as568", row.size, 0.0, json::object());
        REQUIRE(pb.ops.size() == 2);
        const json* sketch = find_feat(pb.ops, "sketch");
        const json* revolve = find_feat(pb.ops, "revolve");
        REQUIRE(sketch);
        REQUIRE(revolve);
        CHECK((*sketch)["refs"]["plane"] == "plane.front");
        const json& entities = (*sketch)["params"]["entities"];
        const json& constraints = (*sketch)["params"]["constraints"];
        REQUIRE(entities.size() == 2);
        REQUIRE(constraints.size() == 6);
        const json& circle = entities[0];
        CHECK(circle["type"] == "circle");
        CHECK_FALSE(circle["construction"].get<bool>());
        const double center = circle["center"][0].get<double>();
        const double radius = circle["radius"].get<double>();
        const double id = row.id_mils * (25.4 / 1000.0);
        const double section = row.cs_mils * (25.4 / 1000.0);
        CHECK(circle["center"][1] == 0.0);
        CHECK(radius == doctest::Approx(section / 2.0));
        CHECK(2.0 * (center - radius) == doctest::Approx(id).epsilon(1e-12));
        CHECK(2.0 * (center + radius) == doctest::Approx(id + 2.0 * section).epsilon(1e-12));
        const json& inner_diameter = entities[1];
        CHECK(inner_diameter["type"] == "line");
        CHECK(inner_diameter["construction"].get<bool>());
        CHECK(inner_diameter["p1"][0].get<double>() == doctest::Approx(-id / 2.0));
        CHECK(inner_diameter["p2"][0].get<double>() == doctest::Approx(id / 2.0));
        CHECK(inner_diameter["p1"][1] == 0.0);
        CHECK(inner_diameter["p2"][1] == 0.0);
        CHECK(constraints[0]["type"] == "point_on");
        CHECK(constraints[0]["refs"] == json::array({"e1.center", "axis.h"}));
        CHECK(constraints[1]["type"] == "midpoint");
        CHECK(constraints[1]["refs"] == json::array({"origin", "e2"}));
        CHECK(constraints[2]["type"] == "horizontal");
        CHECK(constraints[2]["refs"] == json::array({"e2"}));
        CHECK(constraints[3]["type"] == "point_on");
        CHECK(constraints[3]["refs"] == json::array({"e2.p2", "e1"}));
        CHECK(constraints[4]["type"] == "length");
        CHECK(constraints[4]["refs"] == json::array({"e2"}));
        CHECK(constraints[4]["value"]["value"].get<double>() == doctest::Approx(id));
        CHECK(constraints[5]["type"] == "diameter");
        CHECK(constraints[5]["refs"] == json::array({"e1"}));
        CHECK(constraints[5]["value"]["value"].get<double>() == doctest::Approx(section));
        CHECK((*revolve)["params"]["angle"] == 360);
        CHECK((*revolve)["params"]["mode"] == "boss");
        CHECK(((*revolve)["refs"]["axis"] == json{{"kind", "datum"}, {"feature", "axis.z"}}));
        const json& profile = (*revolve)["refs"]["profile"][0];
        CHECK(profile["kind"] == "region");
        CHECK(profile["sketch"] == "$0");
        CHECK(profile["centroid"][0].get<double>() == doctest::Approx(center));
        CHECK(profile["centroid"][1] == 0.0);
        CHECK(pb.meta["specs"]["id_mm"].get<double>() == doctest::Approx(id));
        CHECK(pb.meta["specs"]["cs_mm"].get<double>() == doctest::Approx(section));
        CHECK(pb.meta["specs"]["od_mm"].get<double>() == doctest::Approx(id + 2.0 * section));
    }
}

TEST_CASE("AS568 dimensions use the published inch data without millimetre rounding") {
    // Independent nominal spot checks span all five series and all special
    // cross-sections, including source-table discontinuities near the end.
    const struct { const char* size; double id_in, cs_in; } cases[] = {
        {"AS568-001", 0.029, 0.040}, {"AS568-002", 0.042, 0.050},
        {"AS568-003", 0.056, 0.060}, {"AS568-004", 0.070, 0.070},
        {"AS568-050", 5.239, 0.070}, {"AS568-102", 0.049, 0.103},
        {"AS568-178", 9.737, 0.103}, {"AS568-201", 0.171, 0.139},
        {"AS568-284", 17.955, 0.139}, {"AS568-309", 0.412, 0.210},
        {"AS568-319", 1.034, 0.210}, {"AS568-390", 20.955, 0.210},
        {"AS568-425", 4.475, 0.275}, {"AS568-461", 15.955, 0.275},
        {"AS568-467", 18.955, 0.275},
    };
    for (const auto& c : cases) {
        INFO(c.size);
        const PartBuild pb = build("oring_as568", c.size, 0.0, json::object());
        const json& specs = pb.meta["specs"];
        CHECK(specs["id_in"].get<double>() == doctest::Approx(c.id_in));
        CHECK(specs["cs_in"].get<double>() == doctest::Approx(c.cs_in));
        CHECK(specs["id_mm"].get<double>() == doctest::Approx(c.id_in * 25.4));
        CHECK(specs["cs_mm"].get<double>() == doctest::Approx(c.cs_in * 25.4));
    }
    const json specs = build("oring_as568", "AS568-001", 0.0, json::object()).meta["specs"];
    CHECK(specs["id_mm"].get<double>() == doctest::Approx(0.7366).epsilon(1e-12));
    CHECK(specs["cs_mm"].get<double>() == doctest::Approx(1.016).epsilon(1e-12));
    CHECK(specs["od_mm"].get<double>() == doctest::Approx(2.7686).epsilon(1e-12));
    for (const char* unknown : {"AS568-000", "AS568-051", "AS568-101", "AS568-468",
                                "AS568-1", "001", "AS568-001x"})
        CHECK_THROWS_AS(build("oring_as568", unknown, 0.0, json::object()), std::invalid_argument);
}

TEST_CASE("dowel diameter requires a complete finite number") {
    CHECK(build("dowel_pin", " 6.5 ", 10, json::object()).meta["specs"]["diameter"] == 6.5);
    for (const char* size : {"6/../../victim", "6mm", "6x", "nan", "inf", "1e999"}) {
        CHECK_THROWS_AS(build("dowel_pin", size, 10, json::object()), std::invalid_argument);
    }
}

TEST_CASE("SHCS metric M3x10 — shank + head at exact ISO 4762 dims") {
    PartBuild pb = build("shcs", "M3", 10.0, json::object());
    CHECK(pb.ops.size() == 4);
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
    CHECK(pb.ops.size() == 4);
    const json* shank = find_feat(pb.ops, "cylinder");
    REQUIRE(shank);
    CHECK(cyl_radius(*shank) == doctest::Approx(6.35 / 2.0));
}

TEST_CASE("SHCS editable sockets use tabulated key widths and minimum depths") {
    // ISO 4762 / DIN 912 and ASME B18.3 tables cited in catalog.cpp.
    const struct { const char* size; double width, depth, head_h; } cases[] = {
        {"M1.6", 1.5, 0.7, 1.6}, {"M2", 1.5, 1.0, 2.0},
        {"M2.5", 2.0, 1.1, 2.5}, {"M3", 2.5, 1.3, 3.0},
        {"M4", 3.0, 2.0, 4.0}, {"M5", 4.0, 2.5, 5.0},
        {"M6", 5.0, 3.0, 6.0}, {"M8", 6.0, 4.0, 8.0},
        {"M10", 8.0, 5.0, 10.0}, {"M12", 10.0, 6.0, 12.0},
        {"#6-32", 7.0/64*25.4, 0.064*25.4, 3.51},
        {"#8-32", 9.0/64*25.4, 0.077*25.4, 4.17},
        {"#10-24", 5.0/32*25.4, 0.090*25.4, 4.83},
        {"1/4-20", 3.0/16*25.4, 0.120*25.4, 6.35},
        {"5/16-18", 1.0/4*25.4, 0.151*25.4, 7.94},
        {"3/8-16", 5.0/16*25.4, 0.182*25.4, 9.53},
        {"1/2-13", 3.0/8*25.4, 0.245*25.4, 12.70},
    };
    for (const auto& c : cases) {
        INFO(c.size);
        const PartBuild pb = build("shcs", c.size, 20, json::object());
        const json* sketch = find_feat(pb.ops, "sketch");
        const json* cut = find_feat(pb.ops, "extrude");
        REQUIRE(sketch);
        REQUIRE(cut);
        const auto& p = (*sketch)["params"];
        REQUIRE(p["entities"].size() == 6);
        CHECK(p["constraints"].size() == 6);
        const json& shank = pb.ops[0]["feature"]["params"];
        const json& head = pb.ops[1]["feature"]["params"];
        CHECK(shank["at"][2].get<double>() == doctest::Approx(0.0));
        CHECK(shank["height"]["value"].get<double>() == doctest::Approx(20.0));
        CHECK(head["at"][2].get<double>() == doctest::Approx(20.0));
        if (std::string(c.size) == "M5") {
            CHECK(shank["radius"]["value"].get<double>() == doctest::Approx(2.5));
            CHECK(head["radius"]["value"].get<double>() == doctest::Approx(4.25));
            CHECK(head["height"]["value"].get<double>() == doctest::Approx(5.0));
        }
        double xmin = std::numeric_limits<double>::infinity();
        double xmax = -xmin;
        for (const auto& line : p["entities"]) {
            for (const char* endpoint : {"p1", "p2"}) {
                const double x = line[endpoint][0].get<double>();
                xmin = std::min(xmin, x);
                xmax = std::max(xmax, x);
            }
        }
        CHECK(xmax - xmin == doctest::Approx(c.width));
        CHECK(p["offset"]["value"].get<double>() == doctest::Approx(20 + c.head_h - c.depth));
        CHECK((*cut)["params"]["mode"] == "cut");
        CHECK((*cut)["params"]["depth"]["value"].get<double>() == doctest::Approx(c.depth));
        CHECK((*cut)["refs"]["profile"][0]["sketch"] == "$2");
        CHECK(pb.meta["specs"]["socket_depth_basis"] == "minimum full-hex depth");
    }
}

TEST_CASE("SHCS placement returns and persists geometry quality and omissions") {
    PlaceSeams seams;
    seams.parts_make = [](const json&) {
        return json{{"ok", true}, {"doc_id", "doc1"}, {"name", "M5"},
                    {"meta", build("shcs", "M5", 10, json::object()).meta}};
    };
    json persisted;
    seams.set_catalog_meta = [&](const std::string& doc, const json& meta) {
        CHECK(doc == "doc1");
        persisted = meta;
    };
    json result;
    std::string err;
    const json record{{"geometry_ref", {{"kind", "core_family"}, {"family", "shcs"},
                                        {"size", "M5"}, {"length_mm", 10}}}};
    REQUIRE(place_call("part_place", {{"part_id", "m5"}, {"record", record}},
                       result, err, seams));
    REQUIRE(result["ok"].get<bool>());
    const json& detail = result["detail"];
    CHECK(detail["geometry_quality"] == "nominal_generated_envelope");
    CHECK(detail["geometry_quality"] == persisted["geometry_quality"]);
    CHECK(detail["omissions"] == persisted["omissions"]);
    CHECK(std::find(detail["omissions"].begin(), detail["omissions"].end(), json("threads"))
          != detail["omissions"].end());
    CHECK(detail["omissions"].dump().find("certified") != std::string::npos);
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
        REQUIRE(pb.ops.size() == 6);
        const double radial_width = (row->od - row->bore) / 2.0;
        const double inner_r = row->bore / 2.0 + 0.25 * radial_width;
        const double outer_r = row->od / 2.0 - 0.25 * radial_width;
        const double depth = std::min(0.3, 0.04 * row->width);
        for (int face = 0; face < 2; ++face) {
            const json& recess = pb.ops[1 + 2 * face]["feature"];
            const json& race = pb.ops[2 + 2 * face]["feature"];
            CHECK(recess["params"]["mode"] == "cut");
            CHECK(race["params"]["mode"] == "boss");
            CHECK(cyl_radius(recess) == doctest::Approx(outer_r));
            CHECK(cyl_radius(race) == doctest::Approx(inner_r));
            CHECK(recess["params"]["height"]["value"].get<double>() == doctest::Approx(depth));
            CHECK(recess["params"]["at"][2].get<double>() ==
                  doctest::Approx(face == 0 ? 0.0 : row->width - depth));
        }
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

TEST_CASE("MG996R and MG995 emit stepped housings, open slots and 25-tooth output") {
    for (const char* size : {"MG996R", "MG995"}) {
        INFO(size);
        const PartBuild pb = build("hobby_servo", size, 0, json::object());
        auto named = [&](const std::string& name) -> const json* {
            for (const auto& op : pb.ops)
                if (op["feature"].value("name", "") == name) return &op["feature"];
            return nullptr;
        };
        const json* flange = named("mounting flange");
        const json* sketch = named("output spline profile");
        const json* spline = named("output spline");
        const json* bore = named("output retaining screw tap bore");
        REQUIRE(flange);
        REQUIRE(sketch);
        REQUIRE(spline);
        REQUIRE(bore);
        CHECK((*flange)["params"]["at"][2].get<double>() == doctest::Approx(26.8));
        CHECK((*flange)["params"]["height"]["value"].get<double>() == doctest::Approx(2.5));
        for (const char* row : {" -Y", " +Y"}) {
            const json* left = named(std::string("mount hole -X") + row);
            const json* right = named(std::string("mount hole +X") + row);
            const json* left_opening = named(std::string("slot opening -X") + row);
            const json* right_opening = named(std::string("slot opening +X") + row);
            REQUIRE(left);
            REQUIRE(right);
            REQUIRE(left_opening);
            REQUIRE(right_opening);
            const double lx = (*left)["params"]["at"][0].get<double>();
            const double rx = (*right)["params"]["at"][0].get<double>();
            const double expected_y = row[1] == '-' ? -5.0 : 5.0;
            CHECK(rx - lx == doctest::Approx(49.5));
            CHECK((*left)["params"]["at"][1].get<double>() == doctest::Approx(expected_y));
            CHECK((*right)["params"]["at"][1].get<double>() == doctest::Approx(expected_y));
            CHECK(cyl_radius(*left) == doctest::Approx(2.4));
            CHECK(cyl_radius(*right) == doctest::Approx(2.4));
            for (const json* opening : {left_opening, right_opening}) {
                CHECK((*opening)["params"]["mode"] == "cut");
                CHECK((*opening)["params"]["width"]["value"].get<double>() == doctest::Approx(3.2));
                CHECK((*opening)["params"]["at"][1].get<double>() == doctest::Approx(expected_y - 1.6));
                const double x0 = (*opening)["params"]["at"][0].get<double>();
                const double x1 = x0 + (*opening)["params"]["length"]["value"].get<double>();
                // The cut crosses the original tab end rather than ending at the bore.
                const bool crosses_tab_end = (x0 < -27.0 && x1 >= lx) || (x0 <= rx && x1 > 27.0);
                CHECK(crosses_tab_end);
            }
        }
        REQUIRE(named("bottom cover"));
        REQUIRE(named("top cap"));
        REQUIRE(named("upper output boss"));
        REQUIRE(named("secondary gear bump"));
        const json* cable = named("cable strain relief");
        REQUIRE(cable);
        const double shaft_x = pb.meta["output_axis"]["origin"][0].get<double>();
        CHECK((*cable)["params"]["at"][0].get<double>() > shaft_x);
        CHECK((*cable)["params"]["at"][2].get<double>() == doctest::Approx(2.0));
        CHECK(pb.meta["output_axis"]["origin"][2].get<double>() == doctest::Approx(40.0));
        CHECK(pb.meta["output_axis"]["axis"] == json::array({0, 0, 1}));
        CHECK(pb.meta["specs"]["spline_teeth"] == 25);
        CHECK((*sketch)["params"]["offset"]["value"].get<double>() == doctest::Approx(40.0));
        const json& lines = (*sketch)["params"]["entities"];
        REQUIRE(lines.size() == 75);  // 25 roots and 25 two-vertex crests
        CHECK((*sketch)["params"]["constraints"].size() == lines.size());
        for (std::size_t i = 0; i < lines.size(); ++i) {
            const json& p = lines[i]["p1"];
            const double radius = std::hypot(p[0].get<double>() - shaft_x, p[1].get<double>());
            CHECK(radius == doctest::Approx(i % 3 == 0 ? 2.75 : 2.95));
            CHECK(lines[i]["p2"] == lines[(i + 1) % lines.size()]["p1"]);
        }
        CHECK((*spline)["params"]["depth"]["value"].get<double>() == doctest::Approx(2.9));
        CHECK((*bore)["params"]["at"][0].get<double>() == doctest::Approx(shaft_x));
        CHECK((*bore)["params"]["at"][2].get<double>() == doctest::Approx(38.4));
        CHECK(cyl_radius(*bore) == doctest::Approx(1.25));
    }
    const PartBuild pb = build("hobby_servo", "MG996R", 0, json::object());
    CHECK(pb.meta["output_axis"]["origin"][0].get<double>() == doctest::Approx(10.45));
}

TEST_CASE("every advertised hobby servo size builds without assuming an interchangeable spline") {
    const json cat = catalog_json();
    for (const json& family : cat["families"]) {
        if (family["key"] != "hobby_servo") continue;
        for (const std::string size : family["sizes"].get<std::vector<std::string>>()) {
            INFO(size);
            const PartBuild pb = build("hobby_servo", size, 0, json::object());
            if (size == "MG996R" || size == "MG995")
                CHECK(pb.ops.size() > 6);
            else
                CHECK(pb.ops.size() == 6);
            CHECK(pb.meta["specs"]["source"].is_string());
            CHECK(pb.meta["output_axis"]["axis"] == json::array({0, 0, 1}));
        }
    }
    CHECK_THROWS_AS(build("hobby_servo", "UNKNOWN", 0, json::object()), std::invalid_argument);
}
