// End-to-end checks for native catalog recipes, including their actual B-reps.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include "structural/sections.h"
#include "parts/place.h"
#include "structural/estimate.h"
#include "structural/model.h"

// AISC Shapes Database v16.0 published inch values; SI tables are unrounded
// conversions. The tolerance admits the published display rounding.
TEST_CASE("AISC W S and C section dimensions and analysis properties match published values") {
    struct Published {
        const char* name;
        double d, bf, tw, tf, area, ix;
    };
    const Published shapes[] = {
        {"W4X13", 4.16, 4.06, 0.280, 0.345, 3.83, 11.3},
        {"W8X31", 8.00, 8.00, 0.285, 0.435, 9.13, 110.0},
        {"S8X23", 8.00, 4.17, 0.441, 0.425, 6.76, 64.7},
        {"C6X13", 6.00, 2.16, 0.437, 0.343, 3.82, 17.3},
    };
    constexpr double inch = 25.4;
    for (const auto& s : shapes) {
        INFO(s.name);
        const auto row = chudware::structural::get_aisc_section(s.name);
        REQUIRE(row.has_value());
        CHECK((*row)["d"].get<double>() == doctest::Approx(s.d * inch).epsilon(0.003));
        CHECK((*row)["bf"].get<double>() == doctest::Approx(s.bf * inch).epsilon(0.003));
        CHECK((*row)["tw"].get<double>() == doctest::Approx(s.tw * inch).epsilon(0.003));
        CHECK((*row)["tf"].get<double>() == doctest::Approx(s.tf * inch).epsilon(0.003));
        CHECK((*row)["A"].get<double>() == doctest::Approx(s.area * inch * inch).epsilon(0.003));
        CHECK((*row)["Ix"].get<double>() ==
              doctest::Approx(s.ix * inch * inch * inch * inch).epsilon(0.003));
    }
}

TEST_CASE("structural workspace catalog placements remain usable by analysis and takeoff") {
    using nlohmann::json;
    using namespace chudware::structural;
    const json doc{{"schema", "aicad.structure/1"}, {"name", "catalog sections"},
                   {"units", "mm"}, {"elements", json::array()}, {"load_cases", json::array()}};
    auto placed = [&](const std::string& standard, const std::string& designation) {
        json result;
        std::string error;
        const json ref{{"kind", "section_profile"}, {"standard", standard},
                       {"designation", designation}};
        const json request{{"part_id", "section:" + standard + ":" + designation},
                           {"record", {{"geometry_ref", ref}}},
                           {"doc", doc}, {"params", {{"id", "b1"}, {"length_mm", 3048.0}}}};
        REQUIRE(chudware::parts::place_call("part_place", request, result, error, {}));
        REQUIRE(result["ok"] == true);
        return structure_doc_from_dict(result["doc"]).elements.at(0);
    };
    const auto channel = placed("AISC360", "C6x13");
    const auto props = section_properties(channel.section_name, channel.material);
    const auto channel_catalog = get_aisc_section("C6X13");
    REQUIRE(channel_catalog.has_value());
    CHECK(props.A == doctest::Approx((*channel_catalog)["A"].get<double>()));
    CHECK(props.Iz == doctest::Approx((*channel_catalog)["Ix"].get<double>()));
    const auto beam = placed("AISC360", "W8x31");
    CHECK(steel_weight_lbs(beam) == doctest::Approx(310.0));
    const auto lumber = placed("NDS", "2x4");
    CHECK(lumber.material == "lumber");
    CHECK(section_properties(lumber.section_name, lumber.material).A == doctest::Approx(38.0 * 89.0));
    CHECK(board_feet(lumber) == doctest::Approx(6.6667));
}

#ifdef CHUDWARE_PARTS_NATIVE_TESTS
#include "api/libchudware.h"
#include <nlohmann/json.hpp>
#include <algorithm>
#include <cmath>
#include <string>

using nlohmann::json;

static json rpc(const char* method, const json& params) {
    char* raw = nullptr;
    const std::string encoded = params.dump();
    const int rc = chudware_call(method, encoded.c_str(), &raw);
    const std::string response = raw ? raw : "null";
    chudware_free(raw);
    INFO(method << " rc=" << rc << " response=" << response);
    REQUIRE(rc == 0);
    return json::parse(response);
}

static json record(const char* part_id, const char* domain, const json& ref,
                   const json& params = json::object()) {
    return {{"part_id", part_id}, {"domain", domain}, {"title", part_id},
            {"geometry_ref", ref}, {"params", params}};
}

static std::string place(const json& rec, const json& params = json::object()) {
    const json result = rpc("part_place", {{"part_id", rec["part_id"]}, {"record", rec},
                                          {"params", params}});
    REQUIRE_MESSAGE(result.value("ok", false), result.dump());
    REQUIRE(result["editable"] == true);
    REQUIRE(result["doc_id"] == result["entity_id"]);
    return result["doc_id"].get<std::string>();
}

static void check_solid(const std::string& id, double volume) {
    const json integrity = rpc("eval_model_integrity", {{"doc_id", id}});
    REQUIRE_MESSAGE(integrity["complete"] == true, integrity.dump());
    REQUIRE(integrity["solids"].size() == 1);
    CHECK(integrity["solids"][0]["brep_valid"] == true);
    const json mass = rpc("eval_mass", {{"doc_id", id}});
    CHECK(mass["volume_mm3"].get<double>() == doctest::Approx(volume).epsilon(1e-5));
}

static double extent(const json& bbox, int axis) {
    return bbox["max"][axis].get<double>() - bbox["min"][axis].get<double>();
}

TEST_CASE("AS568-001 is an exact editable torus") {
    const json rec = record("seal:AS568-001:nbr", "seal",
                            {{"kind", "core_family"}, {"family", "oring_as568"},
                             {"size", "AS568-001"}});
    const std::string id = place(rec);
    const double bore = 0.029 * 25.4;
    const double cs = 0.040 * 25.4;
    const double od = bore + 2.0 * cs;
    const double pi = std::acos(-1.0);
    const json bbox = rpc("doc_bbox", {{"doc_id", id}});
    CHECK(extent(bbox, 0) == doctest::Approx(od).epsilon(1e-5));
    CHECK(extent(bbox, 1) == doctest::Approx(od).epsilon(1e-5));
    CHECK(extent(bbox, 2) == doctest::Approx(cs).epsilon(1e-5));
    check_solid(id, 2.0 * pi * pi * ((bore + cs) / 2.0) * (cs / 2.0) * (cs / 2.0));
    const json features = rpc("doc_tree", {{"doc_id", id}})["tree"]["features"];
    REQUIRE(features.is_array());
    CHECK(std::any_of(features.begin(), features.end(), [](const json& f) {
        return f.value("type", std::string()) == "sketch";
    }));
    CHECK(std::any_of(features.begin(), features.end(), [](const json& f) {
        return f.value("type", std::string()) == "revolve";
    }));
    rpc("doc_close", {{"doc_id", id}});
}

TEST_CASE("B16.5 NPS half class 150 flange has catalog-sized bore and bolt holes") {
    const json rec = record("fitting:flange:NPS0.5:cl150", "fitting",
                            {{"kind", "fitting_id"}, {"fitting_id", "flange:NPS0.5:cl150"}},
                            {{"nps", 0.5}, {"flange_class", 150}, {"od", 88.9},
                             {"thickness", 11.18}, {"bolt_circle", 60.45},
                             {"bolt_holes", 4}, {"bolt_dia", 15.75}});
    const std::string id = place(rec);
    const json bbox = rpc("doc_bbox", {{"doc_id", id}});
    CHECK(extent(bbox, 0) == doctest::Approx(88.9).epsilon(1e-5));
    CHECK(extent(bbox, 1) == doctest::Approx(88.9).epsilon(1e-5));
    CHECK(extent(bbox, 2) == doctest::Approx(11.18).epsilon(1e-5));
    const double pi = std::acos(-1.0);
    const double volume = pi / 4.0 * (88.9 * 88.9 - 15.76 * 15.76 - 4.0 * 15.75 * 15.75) * 11.18;
    check_solid(id, volume);
    const json meta = rpc("doc_get_meta", {{"doc_id", id}, {"key", "parts_catalog"}})["value"];
    const json specs = meta["generated"]["specs"];
    CHECK(specs["bolt_circle"].get<double>() == doctest::Approx(60.45));
    CHECK(specs["bolt_holes"] == 4);
    CHECK(specs["bolt_dia"].get<double>() == doctest::Approx(15.75));
    CHECK(specs["bore_mm"].get<double>() == doctest::Approx(15.76));
    CHECK(specs["schedule"] == "40");
    CHECK(specs["schedule_assumed"] == true);
    rpc("doc_close", {{"doc_id", id}});
}

TEST_CASE("B16.9 elbow and reducer retain their actual bores") {
    const double pi = std::acos(-1.0);
    const json elbow = record("fitting:elbow:NPS0.5", "fitting",
                              {{"kind", "fitting_id"}, {"fitting_id", "elbow:NPS0.5"}});
    const std::string elbow_id = place(elbow);
    const double annulus = pi / 4.0 * (21.3 * 21.3 - 15.76 * 15.76);
    check_solid(elbow_id, annulus * (pi / 2.0) * 38.1);
    rpc("doc_close", {{"doc_id", elbow_id}});

    const json reducer = record("fitting:reducer:NPS2.0x1.5", "fitting",
                                {{"kind", "fitting_id"},
                                 {"fitting_id", "reducer:NPS2.0x1.5"}});
    const std::string reducer_id = place(reducer);
    const auto squares = [](double d1, double d2) {
        return d1 * d1 + d1 * d2 + d2 * d2;
    };
    const double volume = pi * 76.2 / 12.0 *
                          (squares(60.3, 48.3) - squares(52.48, 40.94));
    check_solid(reducer_id, volume);
    rpc("doc_close", {{"doc_id", reducer_id}});
}

TEST_CASE("AISC C6x13 is an editable channel extrusion at the requested length") {
    const json rec = record("section:AISC360:C6x13", "section",
                            {{"kind", "section_profile"}, {"standard", "AISC360"},
                             {"designation", "C6x13"}});
    const std::string id = place(rec, {{"length_mm", 400.0}});
    const json bbox = rpc("doc_bbox", {{"doc_id", id}});
    const double x = extent(bbox, 0), y = extent(bbox, 1);
    CHECK(std::min(x, y) == doctest::Approx(2.16 * 25.4).epsilon(1e-5));
    CHECK(std::max(x, y) == doctest::Approx(152.4).epsilon(1e-5));
    CHECK(extent(bbox, 2) == doctest::Approx(400.0).epsilon(1e-5));
    const double bf = 2.16 * 25.4, tf = 0.343 * 25.4, tw = 0.437 * 25.4;
    const double area = 2.0 * bf * tf + tw * (152.4 - 2.0 * tf);
    check_solid(id, area * 400.0);
    const json features = rpc("doc_tree", {{"doc_id", id}})["tree"]["features"];
    REQUIRE(features.is_array());
    const auto extrusion = std::find_if(features.begin(), features.end(), [](const json& f) {
        return f.value("type", std::string()) == "extrude";
    });
    REQUIRE(extrusion != features.end());
    const json edited = rpc("doc_apply_ops", {{"doc_id", id}, {"ops", json::array({
        {{"op", "edit_feature"}, {"id", extrusion->at("id")}, {"params", {{"depth", 550.0}}}}
    })}});
    REQUIRE_FALSE(edited.contains("rejected_tree"));
    const json edited_bbox = rpc("doc_bbox", {{"doc_id", id}});
    CHECK(extent(edited_bbox, 2) == doctest::Approx(550.0).epsilon(1e-5));
    check_solid(id, area * 550.0);
    rpc("doc_close", {{"doc_id", id}});
    const std::string default_id = place(rec);
    const json default_bbox = rpc("doc_bbox", {{"doc_id", default_id}});
    CHECK(extent(default_bbox, 2) == doctest::Approx(3.0 * 152.4).epsilon(1e-5));
    rpc("doc_close", {{"doc_id", default_id}});
}
#endif
