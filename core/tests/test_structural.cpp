// Behavioral and analytic regression tests for core/structural.
//
// Closed-form cantilever, simply-supported beam, and released-truss anchors
// defend units, section axes, stiffness, reactions, equilibrium, support
// constraints, instability handling, utilization, and takeoff completeness.

#include <cmath>
#include <fstream>
#include <sstream>
#include <limits>
#include <string>

#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "structural/checks.h"
#include "structural/estimate.h"
#include "structural/model.h"
#include "structural/sections.h"
#include "structural/solver.h"
#include "structural/structural_rpc.h"

using nlohmann::json;
using namespace chudware::structural;

namespace {

std::string fixture_path(const std::string& name) {
    return std::string(CHUDWARE_TEST_DATA) + "/" + name;
}

json load_fixture(const std::string& name) {
    std::ifstream fh(fixture_path(name));
    REQUIRE(fh.good());
    std::stringstream ss;
    ss << fh.rdbuf();
    return json::parse(ss.str());
}

json rpc(const std::string& method, const json& params) {
    json result;
    std::string err;
    const bool handled = chudware::structural::call(method, params, result, err);
    REQUIRE(handled);
    if (method == "struct_analyze" && result.contains("results")) {
        for (const json& row : result["results"]) {
            const double deflection_y = row["deflection_y_mm"].get<double>();
            const double deflection_z = row["deflection_z_mm"].get<double>();
            const double expected =
                std::abs(deflection_y) >= std::abs(deflection_z) ? deflection_y : deflection_z;
            INFO("structural result=" << row.dump());
            CHECK(row["deflection_mm"].get<double>() == expected);
        }
    }
    return result;
}

// Relative tolerance for solver parity (linear statics): 1e-6 with a small
// absolute floor for near-zero channels.
void check_rel(double actual, double expected, double rel = 1e-6, double abs_floor = 1e-9) {
    const double tol = rel * std::max(std::abs(expected), 1.0) + abs_floor;
    REQUIRE(std::abs(actual - expected) <= tol);
}

const json* row_for(const json& results, const std::string& element_id) {
    for (const json& r : results) {
        if (r.value("element_id", "") == element_id) return &r;
    }
    return nullptr;
}

}  // namespace

// ---------------------------------------------------------------------------
// materials + section geometry (mirrors oracle test_section_properties_*)
// ---------------------------------------------------------------------------

TEST_CASE("material properties reject unknown keys instead of assuming softwood") {
    CHECK(material_properties("steel_a36").E == 200000.0);
    CHECK(material_properties("df_no2").E == 11000.0);
    CHECK(material_properties("cedar").E == 12000.0);
    CHECK(material_properties("cedar").G == 750.0);
    CHECK_THROWS_AS(material_properties("something_else"), std::invalid_argument);
}

TEST_CASE("section properties rectangle") {
    const SectionProps p = section_properties("100x100", "cedar");
    CHECK(p.E == 12000.0);
    CHECK(p.G == 750.0);
    CHECK(p.A == doctest::Approx(1e4));
    CHECK(p.Iz == doctest::Approx(100 * 100 * 100 * 100 / 12.0));
}

TEST_CASE("analysis properties use net catalog and hollow geometry") {
    const SectionProps w = section_properties("W200x100", "steel");
    CHECK(w.A == doctest::Approx(2.0 * 100.0 * 8.0 + (200.0 - 16.0) * 5.5));
    const SectionProps ig = section_properties("I200x100x5.5x8", "steel");
    CHECK(ig.A == doctest::Approx(w.A));

    const SectionProps aisc = section_properties("W8X31", "steel_a992");
    const auto aisc_table = get_aisc_section("W8X31");
    REQUIRE(aisc_table.has_value());
    CHECK(aisc.A == doctest::Approx((*aisc_table)["A"].get<double>()));
    CHECK(aisc.Iz == doctest::Approx((*aisc_table)["Ix"].get<double>()));

    const SectionProps lumber = section_properties("2x4", "df_no2");
    CHECK(lumber.A == doctest::Approx(38.0 * 89.0));
    const SectionProps rhs = section_properties("RHS100x50x4", "steel");
    CHECK(rhs.A == doctest::Approx(100.0 * 50.0 - 92.0 * 42.0));
    CHECK(rhs.Iz < section_properties("100x50", "steel").Iz);
    CHECK_THROWS_AS(section_properties("RHS100x50x30", "steel"), std::invalid_argument);
}

TEST_CASE("load combo presets") {
    CHECK(get_combo("dead_live").name == "DEAD_LIVE");
    CHECK(get_combo("DEAD_LIVE_WIND").wind == 0.5);
    CHECK(get_combo("DEAD_ONLY").factors().size() == 1);
    CHECK_THROWS_AS(get_combo("NOT_A_COMBO"), std::out_of_range);
}

// ---------------------------------------------------------------------------
// section libraries (mirrors TestGetAiscSection / TestGetLumberSection)
// ---------------------------------------------------------------------------

TEST_CASE("AISC section lookup") {
    const auto s = get_aisc_section("w8x31");  // case-insensitive
    REQUIRE(s.has_value());
    CHECK((*s)["A"].get<double>() == doctest::Approx(5884).epsilon(0.02));
    CHECK((*s)["Sx"].get<double>() == doctest::Approx(4.506e5).epsilon(0.02));
    CHECK((*s)["Ix"].get<double>() == doctest::Approx(4.574e7).epsilon(0.02));
    for (const char* k : {"A", "d", "bf", "tf", "tw", "Ix", "Sx", "Zx", "Iy", "Sy", "Zy", "J", "Cw"}) {
        CHECK(s->contains(k));
    }
    CHECK(!get_aisc_section("W99X999").has_value());
}

TEST_CASE("lumber section lookup") {
    const auto s = get_lumber_section("2X4");  // case-insensitive
    REQUIRE(s.has_value());
    CHECK((*s)["b_actual_mm"].get<double>() == doctest::Approx(38).epsilon(0.01));
    CHECK((*s)["d_actual_mm"].get<double>() == doctest::Approx(89).epsilon(0.01));
    CHECK((*s)["A"].get<double>() == doctest::Approx(3382));
    CHECK(!get_lumber_section("3x7").has_value());
}

// ---------------------------------------------------------------------------
// solver — hand-calc validations (mirrors the oracle's textbook checks)
// ---------------------------------------------------------------------------

TEST_CASE("axial post 1 kN (acceptance case)") {
    const json out = rpc("struct_analyze", {{"doc_path", fixture_path("post_axial.json")}});
    const json* r = row_for(out["results"], "post1");
    REQUIRE(r != nullptr);
    CHECK((*r)["combo"] == "DEAD_LIVE");  // default combo
    // 1 kN dead case factored 1.2 → 1200 N compression (contract +tension).
    check_rel((*r)["axial_n"].get<double>(), -1200.0);
    CHECK(std::abs((*r)["deflection_mm"].get<double>()) < 1e-3);
}

TEST_CASE("cantilever tip deflection hand-calc") {
    const json doc = load_fixture("cantilever.json");
    const json out = rpc("struct_analyze", {{"doc", doc}, {"load_combo", "DEAD_LIVE"}});
    const json* r = row_for(out["results"], "beam1");
    REQUIRE(r != nullptr);
    INFO("beam1 result=" << r->dump());
    // Factored tip load P = 500 × 1.2 = 600 N; δ_tip = PL³/(3EI), M_fix = PL.
    const SectionProps p = section_properties("150x150", "cedar");
    const double P = 600.0, L = 3000.0;
    const double expected_defl = P * L * L * L / (3.0 * p.E * p.Iz);
    CHECK(std::abs((*r)["deflection_mm"].get<double>()) == doctest::Approx(expected_defl).epsilon(0.02));
    CHECK(std::abs((*r)["moment_nmm"].get<double>()) == doctest::Approx(P * L).epsilon(0.02));
    // Oracle-parity: PyNite reports exactly these (tip station is sampled).
    check_rel((*r)["shear_n"].get<double>(), 600.0000000000003, 1e-9);
    check_rel((*r)["moment_nmm"].get<double>(), 1800000.0000000005, 1e-9);
    check_rel((*r)["deflection_mm"].get<double>(), -10.666666666666668, 1e-9);
}

TEST_CASE("diagonal gravity preserves both normalized load components") {
    json doc = load_fixture("cantilever.json");
    doc["load_cases"][0]["gravity"] = json::array({0, -1, -1});
    const json out = rpc("struct_analyze", {{"doc", doc}, {"load_combo", "DEAD_LIVE"}});
    REQUIRE(!out.contains("code"));
    const json* row = row_for(out["results"], "beam1");
    REQUIRE(row != nullptr);
    const double component = 600.0 / std::sqrt(2.0);
    CHECK(std::abs((*row)["shear_y_n"].get<double>()) ==
          doctest::Approx(component).epsilon(1e-6));
    CHECK(std::abs((*row)["shear_z_n"].get<double>()) ==
          doctest::Approx(component).epsilon(1e-6));
}

TEST_CASE("simply supported rolled beam matches UDL closed form") {
    const double L = 4000.0;
    const double w = 2.0;
    const json all_fixed = json::array({true, true, true, true, true, true});
    const json doc = {
        {"schema", kSchema},
        {"units", "mm"},
        {"elements",
         json::array({
             {
                 {"element_type", "beam"},
                 {"id", "ss1"},
                 {"section_name", "100x200"},
                 {"material", "cedar"},
                 {"length_mm", L},
                 // local section +Y is global +Z: the 200 mm depth is vertical.
                 {"transform", json::array({0, 0, 1, 0, 1, 0, 0, 0,
                                             0, 1, 0, 0, 0, 0, 0, 1})},
                 {"releases", {{"i", json::array({"rz"})}, {"j", json::array({"rz"})}}},
             },
         })},
        {"supports",
         json::array({
             {{"element_id", "ss1"}, {"end", "i"}, {"restraints", all_fixed}},
             {{"element_id", "ss1"}, {"end", "j"}, {"restraints", all_fixed}},
         })},
        {"load_cases",
         json::array({
             {{"kind", "dead"},
              {"gravity", json::array({0, 0, -1})},
              {"distributed", {{"w_n_per_mm", w}}}},
         })},
    };
    const json out = rpc("struct_analyze", {{"doc", doc}, {"load_combo", "DEAD_ONLY"}});
    REQUIRE(!out.contains("code"));
    const json* row = row_for(out["results"], "ss1");
    REQUIRE(row != nullptr);
    const SectionProps section = section_properties("100x200", "cedar");
    const double wf = 1.4 * w;
    const double expected_moment = wf * L * L / 8.0;
    const double expected_deflection =
        5.0 * wf * L * L * L * L / (384.0 * section.E * section.Iz);
    CHECK(std::abs((*row)["moment_z_nmm"].get<double>()) ==
          doctest::Approx(expected_moment).epsilon(2e-3));
    CHECK(std::abs((*row)["deflection_y_mm"].get<double>()) ==
          doctest::Approx(expected_deflection).epsilon(2e-3));
    CHECK(std::abs((*row)["shear_y_n"].get<double>()) ==
          doctest::Approx(wf * L / 2.0).epsilon(2e-3));
    REQUIRE(out["reactions"].size() == 2);
    const double total_reaction =
        out["reactions"][0]["fz_n"].get<double>() +
        out["reactions"][1]["fz_n"].get<double>();
    CHECK(total_reaction == doctest::Approx(wf * L).epsilon(2e-6));
}

TEST_CASE("result row shape matches the contract") {
    const json out = rpc("struct_analyze", {{"doc", load_fixture("post_axial.json")}});
    for (const json& r : out["results"]) {
        for (const char* k : {"element_id", "combo", "axial_n", "shear_n", "moment_nmm",
                              "deflection_mm", "shear_y_n", "shear_z_n", "moment_y_nmm",
                              "moment_z_nmm", "deflection_y_mm", "deflection_z_mm",
                              "dc_ratio", "pass"}) {
            CHECK(r.contains(k));
        }
        CHECK(r["dc_ratio"].is_null());
        CHECK(r["pass"].is_null());
    }
}

// ---------------------------------------------------------------------------
// solver — connected-frame regression
// ---------------------------------------------------------------------------

TEST_CASE("portal frame parity (DEAD_LIVE)") {
    const json out = rpc("struct_analyze",
                         {{"doc", load_fixture("portal.json")}, {"load_combo", "DEAD_LIVE"}});
    REQUIRE(out["results"].size() == 5);
    const json* p1 = row_for(out["results"], "p1");
    const json* p2 = row_for(out["results"], "p2");
    const json* b1 = row_for(out["results"], "b1");
    REQUIRE(p1 != nullptr);
    REQUIRE(p2 != nullptr);
    REQUIRE(b1 != nullptr);
    check_rel((*p1)["axial_n"].get<double>(), -10848.0);
    check_rel((*p1)["shear_n"].get<double>(), -1760.9831581096253);
    check_rel((*p1)["moment_nmm"].get<double>(), 2818795.9579463103);
    check_rel((*p1)["deflection_mm"].get<double>(), 5.995169329386544);
    check_rel((*p2)["axial_n"].get<double>(), -10848.0);
    check_rel((*p2)["moment_nmm"].get<double>(), -2818795.9579463094);
    check_rel((*p2)["deflection_mm"].get<double>(), -5.995169329386544);
    check_rel((*b1)["axial_n"].get<double>(), -1760.9831581096253);
    check_rel((*b1)["shear_n"].get<double>(), 4192.0);
    check_rel((*b1)["moment_nmm"].get<double>(), 2818795.9579463103);
    check_rel((*b1)["deflection_mm"].get<double>(), -90.45101089820633);
}

TEST_CASE("released two-bar truss matches joint-equilibrium closed form") {
    constexpr double a = 2000.0;
    constexpr double c = 0.7071067811865475244;
    const double L = a * std::sqrt(2.0);
    const json releases = {
        {"i", json::array({"rx", "ry", "rz"})},
        {"j", json::array({"rx", "ry", "rz"})},
    };
    const json fixed = json::array({true, true, true, true, true, true});
    const json doc = {
        {"schema", kSchema},
        {"units", "mm"},
        {"elements",
         json::array({
             {{"element_type", "beam"}, {"id", "left"}, {"section_name", "100x100"},
              {"material", "steel"}, {"length_mm", L},
              {"transform", json::array({c, 0, c, -a, 0, 1, 0, 0,
                                         -c, 0, c, 0, 0, 0, 0, 1})},
              {"releases", releases}},
             {{"element_type", "beam"}, {"id", "right"}, {"section_name", "100x100"},
              {"material", "steel"}, {"length_mm", L},
              {"transform", json::array({c, 0, -c, a, 0, 1, 0, 0,
                                         c, 0, c, 0, 0, 0, 0, 1})},
              {"releases", releases}},
         })},
        {"supports",
         json::array({
             {{"element_id", "left"}, {"end", "i"}, {"restraints", fixed}},
             {{"element_id", "right"}, {"end", "i"}, {"restraints", fixed}},
         })},
        {"load_cases",
         json::array({
             {{"kind", "dead"}, {"gravity", json::array({0, 0, -1})},
              {"point", {{"at_mm", L}, {"p_n", 1000.0}}}},
         })},
    };
    const json out = rpc("struct_analyze", {{"doc", doc}, {"load_combo", "DEAD_ONLY"}});
    REQUIRE(!out.contains("code"));
    const json* left = row_for(out["results"], "left");
    const json* right = row_for(out["results"], "right");
    REQUIRE(left != nullptr);
    REQUIRE(right != nullptr);
    // Two 1.4 kN apex loads: 2*N*sin(45°) = 2*1.4 kN.
    const double expected_compression = 1400.0 * std::sqrt(2.0);
    CHECK((*left)["axial_n"].get<double>() ==
          doctest::Approx(-expected_compression).epsilon(2e-6));
    CHECK((*right)["axial_n"].get<double>() ==
          doctest::Approx(-expected_compression).epsilon(2e-6));
    CHECK(std::abs((*left)["moment_nmm"].get<double>()) < 1e-5);
    CHECK(std::abs((*right)["moment_nmm"].get<double>()) < 1e-5);
}

TEST_CASE("steel frame uses real AISC stiffness and finite axis diagnostics") {
    const json out = rpc("struct_analyze", {{"doc", load_fixture("steel_frame.json")},
                                            {"load_combo", "DEAD_LIVE_WIND"}});
    REQUIRE(!out.contains("code"));
    REQUIRE(out["results"].size() == 5);
    CHECK(out["units"]["force"] == "N");
    CHECK(out["units"]["moment"] == "N*mm");
    const json* b1 = row_for(out["results"], "b1");
    REQUIRE(b1 != nullptr);
    for (const char* key : {"axial_n", "shear_y_n", "shear_z_n", "moment_y_nmm",
                            "moment_z_nmm", "deflection_y_mm", "deflection_z_mm"}) {
        CHECK(std::isfinite((*b1)[key].get<double>()));
    }
    // Regression: W8X31 was once parsed as an 8 mm × 31 mm rectangle, producing
    // five-order-of-magnitude fake flexibility.
    CHECK(std::abs((*b1)["deflection_mm"].get<double>()) < 1000.0);
}

TEST_CASE("multi-combo run (list param)") {
    const json out = rpc("struct_analyze", {{"doc", load_fixture("portal.json")},
                                            {"load_combo", json::array({"DEAD_ONLY", "DEAD_LIVE"})}});
    CHECK(out["results"].size() == 10);  // 5 members × 2 combos
}

// ---------------------------------------------------------------------------
// D/C checks (mirrors test_checks.py + the oracle check rows)
// ---------------------------------------------------------------------------

TEST_CASE("check_member steel LRFD hand-calc (W8x31 A992 acceptance case)") {
    const auto section = get_aisc_section("W8X31");
    REQUIRE(section.has_value());
    // φMy = 0.90 × Sx × 344.7; demand at 80% → dc_bending ≈ 0.800.
    const double capacity = 0.90 * (*section)["Sx"].get<double>() * 344.7;
    const json row = {{"element_id", "b1"}, {"combo", "DEAD_LIVE"},
                      {"axial_n", 0.0}, {"moment_nmm", 0.80 * capacity}};
    const CheckResult cr = check_member(row, *section, "steel_a992");
    CHECK(cr.dc_bending == doctest::Approx(0.80).epsilon(1e-9));
    CHECK(cr.governing_limit == "bending_yield_LRFD");
    CHECK(cr.passed);
    CHECK(cr.to_dict()["disclaimer"] == kCheckDisclaimer);
    CHECK_THROWS_AS(dc_ratio_axial(1000.0, 0.0, 248.0), std::invalid_argument);
    CHECK_THROWS_AS(dc_ratio_bending(1000.0, 100.0,
                                     std::numeric_limits<double>::infinity()),
                    std::invalid_argument);
}

TEST_CASE("check_member wood combines axial and bending utilization") {
    const auto section = get_lumber_section("2x4");
    REQUIRE(section.has_value());
    const json row = {{"element_id", "j1"}, {"combo", "DEAD_LIVE"},
                      {"axial_n", -9000.0}, {"moment_nmm", 100000.0}};
    const CheckResult cr = check_member(row, *section, "df_no2");
    CHECK(cr.dc_axial == doctest::Approx(9000.0 / (9.307 * 3382.0)));
    CHECK(cr.dc_bending == doctest::Approx(100000.0 / (6.205 * 50250.0)));
    CHECK(cr.dc_ratio == doctest::Approx(cr.dc_axial + cr.dc_bending));
    CHECK(cr.governing_limit == "combined_axial_bending_ASD_NDS");
}

TEST_CASE("struct_check reports deflection and unavailable footing checks honestly") {
    const json out = rpc("struct_check",
                         {{"doc", load_fixture("portal.json")}, {"load_combo", "DEAD_LIVE"}});
    CHECK(out["disclaimer"] == kCheckDisclaimer);
    CHECK(out["complete"] == false);
    const json* p1 = row_for(out["results"], "p1");
    const json* b1 = row_for(out["results"], "b1");
    const json* f1 = row_for(out["results"], "f1");
    REQUIRE(p1 != nullptr);
    REQUIRE(b1 != nullptr);
    REQUIRE(f1 != nullptr);
    CHECK((*p1)["status"] == "evaluated");
    CHECK((*p1)["dc_ratio"].get<double>() >= (*p1)["dc_strength"].get<double>());
    CHECK((*p1)["dc_deflection"].is_number());
    CHECK((*b1)["deflection_limit_basis"] == "L/240 total-load preliminary");
    CHECK((*f1)["status"] == "unavailable");
    CHECK((*f1)["dc_ratio"].is_null());
    CHECK((*f1)["passed"] == false);
}

TEST_CASE("struct_check steel frame uses AISC biaxial interaction") {
    const json out = rpc("struct_check", {{"doc", load_fixture("steel_frame.json")}});
    const json* c1 = row_for(out["results"], "c1");
    const json* b1 = row_for(out["results"], "b1");
    REQUIRE(c1 != nullptr);
    REQUIRE(b1 != nullptr);
    CHECK((*c1)["status"] == "evaluated");
    CHECK((*b1)["status"] == "evaluated");
    CHECK((*c1)["dc_bending"].get<double>() ==
          doctest::Approx((*c1)["dc_bending_major"].get<double>() +
                          (*c1)["dc_bending_minor"].get<double>()));
    CHECK((*b1)["dc_ratio"].get<double>() >= (*b1)["dc_strength"].get<double>());
}

// ---------------------------------------------------------------------------
// cost takeoff (mirrors test_estimate.py; byte-exact arithmetic)
// ---------------------------------------------------------------------------

TEST_CASE("board feet + steel weight + fasteners") {
    StructureDoc doc;
    doc.elements.push_back({"post", "p1", "4x4", "cedar", 8.0 * 304.8, {}});
    CHECK(board_feet(doc.elements.back()) == doctest::Approx(10.6667).epsilon(1e-9));
    doc.elements.push_back({"joist", "j1", "2x10", "lumber", 12.0 * 304.8, {}});
    CHECK(board_feet(doc.elements.back()) == doctest::Approx(20.0).epsilon(1e-9));
    doc.elements.push_back({"beam", "b1", "W8X31", "steel", 10.0 * 304.8, {}});
    CHECK(steel_weight_lbs(doc.elements.back()) == doctest::Approx(310.0).epsilon(1e-9));
    CHECK_THROWS_AS(board_feet(doc.elements.back()), std::invalid_argument);
    StructuralElement bad{"beam", "b2", "W99X999", "steel", 3000.0, {}};
    CHECK_THROWS_AS(steel_weight_lbs(bad), std::invalid_argument);
    CHECK(fastener_count(doc) == 8 + 2 * 10 + 6);
}

TEST_CASE("struct_estimate mixed doc (oracle byte-parity)") {
    const json out = rpc("struct_estimate", {{"doc", load_fixture("mixed.json")}});
    CHECK(out["currency"] == "USD");
    CHECK(out["rate_table_date"] == "ENR Q4-2025");
    CHECK(out["disclaimer"] == kEstimateDisclaimer);
    REQUIRE(out["line_items"].size() == 4);

    const json& post = out["line_items"][0];
    CHECK(post["quantity"].get<double>() == 10.6667);
    CHECK(post["unit"] == "BF");
    CHECK(post["material_cost"].get<double>() == 48.0);
    CHECK(post["labor_cost"].get<double>() == 85.0);
    CHECK(post["description"] == "Post — 4x4 × 8.00 ft cedar");

    const json& beam = out["line_items"][1];
    CHECK(beam["quantity"].get<double>() == 310.0);
    CHECK(beam["unit"] == "lbs");
    CHECK(beam["material_cost"].get<double>() == 372.0);
    CHECK(beam["labor_hours"].get<double>() == 0.75);
    CHECK(beam["labor_cost"].get<double>() == 63.75);
    CHECK(beam["description"] == "Beam — W8X31 × 10.00 ft steel");

    const json& joist = out["line_items"][2];
    CHECK(joist["quantity"].get<double>() == 20.0);
    CHECK(joist["material_cost"].get<double>() == 90.0);
    CHECK(joist["labor_cost"].get<double>() == 21.25);
    CHECK(joist["description"] == "Joist — 2x10 × 12.00 ft lumber");

    const json& f = out["fastener_line"];
    CHECK(f["quantity"].get<double>() == 54.0);
    CHECK(f["material_cost"].get<double>() == 18.9);
    CHECK(f["description"] == "Structural fasteners — 54 ea (anchor bolts, hangers, screws)");

    CHECK(out["total_material_cost"].get<double>() == 618.9);
    CHECK(out["total_labor_cost"].get<double>() == 191.25);
    CHECK(out["total_cost"].get<double>() == 810.15);
}

TEST_CASE("concrete footing takeoff uses plan area times depth") {
    StructureDoc doc;
    doc.elements.push_back({"footing", "f1", "600x600", "concrete", 200.0, {}});
    const json out = estimate(doc, RateTable{});
    REQUIRE(out["complete"] == true);
    REQUIRE(out["line_items"].size() == 1);
    const double expected_cy = 600.0 * 600.0 * 200.0 /
                               (914.4 * 914.4 * 914.4);
    CHECK(out["line_items"][0]["quantity"].get<double>() ==
          doctest::Approx(expected_cy).epsilon(1e-3));
    CHECK(out["line_items"][0]["material_estimated"] == true);
    RateTable bad_rates;
    bad_rates.concrete_per_cy = -1.0;
    CHECK_THROWS_AS(estimate(doc, bad_rates), std::invalid_argument);
}

// ---------------------------------------------------------------------------
// error envelopes
// ---------------------------------------------------------------------------

TEST_CASE("error envelopes") {
    json result;
    std::string err;
    // Unknown method falls through (the dispatcher routes to the next layer).
    CHECK(!chudware::structural::call("not_a_struct_method", json::object(), result, err));
    // Bad schema.
    result = rpc("struct_analyze", {{"doc", json::object({{"schema", "nope"}})}});
    CHECK(result["code"] == "invalid_schema");
    // Unknown combo.
    result = rpc("struct_analyze",
                 {{"doc", load_fixture("post_axial.json")}, {"load_combo", "NOPE"}});
    CHECK(result["code"] == "unknown_key");
    // Missing doc reference.
    result = rpc("struct_estimate", json::object());
    CHECK(result["code"] == "invalid_schema");
    // Missing file on disk.
    result = rpc("struct_analyze", {{"doc_path", "/nonexistent/structure.json"}});
    CHECK(result["code"] == "solver_error");
    // Zero-length element.
    json bad = load_fixture("post_axial.json");
    bad["elements"][0]["length_mm"] = 0.0;
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "invalid_schema");
    bad = load_fixture("post_axial.json");
    bad["units"] = "m";
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "invalid_schema");
    // Non-finite geometry.
    bad = load_fixture("post_axial.json");
    bad["elements"][0]["length_mm"] = std::numeric_limits<double>::infinity();
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "invalid_schema");
    // Invalid transforms are not silently replaced with identity.
    bad = load_fixture("post_axial.json");
    bad["elements"][0]["transform"] = json::array({1, 0, 0});
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "invalid_schema");
    // Load directions and magnitudes must be physically meaningful.
    bad = load_fixture("cantilever.json");
    bad["load_cases"][0]["gravity"] = json::array({0, 0, 0});
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "invalid_schema");
    bad = load_fixture("cantilever.json");
    bad["load_cases"][0]["point"]["p_n"] = -1.0;
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "invalid_schema");
    // Stable but unexcited studies cannot report reassuring zero demand.
    bad = load_fixture("cantilever.json");
    bad["load_cases"] = json::array();
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "invalid_schema");
    // Mechanisms are detected from stiffness rank rather than a lucky load residual.
    bad = load_fixture("cantilever.json");
    bad["supports"] = json::array({
        {{"element_id", "beam1"}, {"end", "i"},
         {"restraints", json::array({"ux", "uy", "uz"})}},
    });
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "solver_error");
    // Load stations outside any target member fail before assembly.
    bad = load_fixture("cantilever.json");
    bad["load_cases"][0]["point"]["at_mm"] = 3000.1;
    result = rpc("struct_analyze", {{"doc", bad}});
    CHECK(result["code"] == "invalid_schema");
}
