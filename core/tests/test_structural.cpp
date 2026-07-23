// Structural engine tests (T-RF19 leaf 5) — doctest suite for core/structural.
//
// Mirrors engine_oracle/structural/test_analysis.py + test_checks.py +
// test_estimate.py, and pins solver parity against the Python (PyNite 3.0.0)
// oracle on the aicad.structure/1 fixtures in tests/data/structural/: member
// forces/deflections match to relative 1e-6 (linear statics), the D/C checks
// and the cost takeoff are arithmetic — compared exactly.
//
// Golden values in the *_PARITY cases were captured from the oracle
// (scratch/struct_parity/gen_fixtures.py dumps) — see docs/REFACTOR_LOG.md.

#include <cmath>
#include <fstream>
#include <sstream>
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

TEST_CASE("material prefix matching") {
    CHECK(material_properties("steel_a36").E == 200000.0);
    CHECK(material_properties("df_no2").E == 11000.0);
    CHECK(material_properties("cedar").E == 12000.0);
    CHECK(material_properties("cedar").G == 750.0);
    CHECK(material_properties("something_else").E == 9000.0);  // softwood fallback
}

TEST_CASE("section properties rectangle") {
    const SectionProps p = section_properties("100x100", "cedar");
    CHECK(p.E == 12000.0);
    CHECK(p.G == 750.0);
    CHECK(p.A == doctest::Approx(1e4));
    CHECK(p.Iz == doctest::Approx(100 * 100 * 100 * 100 / 12.0));
}

TEST_CASE("section properties named I-shape + I grammar + RHS solid") {
    const SectionProps w = section_properties("W200x100", "steel");
    CHECK(w.A == doctest::Approx(2.0 * 100.0 * 8.0 + (200.0 - 16.0) * 5.5));
    const SectionProps ig = section_properties("I200x100x5.5x8", "steel");
    CHECK(ig.A == doctest::Approx(w.A));
    // RHS keeps the solid outer rectangle in the analysis path (oracle NOTE).
    const SectionProps rhs = section_properties("RHS100x50x4", "steel");
    CHECK(rhs.A == doctest::Approx(100.0 * 50.0));
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

TEST_CASE("axial post 1 kN (DoD case)") {
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

TEST_CASE("result row shape matches the contract") {
    const json out = rpc("struct_analyze", {{"doc", load_fixture("post_axial.json")}});
    for (const json& r : out["results"]) {
        for (const char* k :
             {"element_id", "combo", "axial_n", "shear_n", "moment_nmm", "deflection_mm", "dc_ratio", "pass"}) {
            CHECK(r.contains(k));
        }
        CHECK(r["dc_ratio"].is_null());
        CHECK(r["pass"].is_null());
    }
}

// ---------------------------------------------------------------------------
// solver — PyNite parity on frames (golden rows captured from the oracle)
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

TEST_CASE("truss parity (DEAD_LIVE, sloped members)") {
    const json out = rpc("struct_analyze",
                         {{"doc", load_fixture("truss.json")}, {"load_combo", "DEAD_LIVE"}});
    REQUIRE(out["results"].size() == 6);
    const json* bc1 = row_for(out["results"], "bc1");
    const json* bc2 = row_for(out["results"], "bc2");
    const json* tc1 = row_for(out["results"], "tc1");
    const json* tc2 = row_for(out["results"], "tc2");
    REQUIRE(bc1 != nullptr);
    REQUIRE(tc1 != nullptr);
    check_rel((*bc1)["shear_n"].get<double>(), 8000.0);
    check_rel((*bc1)["moment_nmm"].get<double>(), 7200000.0);
    check_rel((*bc1)["deflection_mm"].get<double>(), -157090909.0909091);
    check_rel((*bc2)["shear_n"].get<double>(), -8000.0);
    check_rel((*tc1)["axial_n"].get<double>(), -10945.304482472699);
    check_rel((*tc1)["shear_n"].get<double>(), 4002.114835236155);
    check_rel((*tc1)["moment_nmm"].get<double>(), 2498539.207513179);
    check_rel((*tc1)["deflection_mm"].get<double>(), -22136328.13607333);
    check_rel((*tc2)["axial_n"].get<double>(), -10520.90834929402);
    check_rel((*tc2)["moment_nmm"].get<double>(), -2429276.7674487364);
}

TEST_CASE("steel frame parity incl. wind combo (I-shape grammar)") {
    const json out = rpc("struct_analyze", {{"doc", load_fixture("steel_frame.json")},
                                            {"load_combo", "DEAD_LIVE_WIND"}});
    const json* c1 = row_for(out["results"], "c1");
    const json* c2 = row_for(out["results"], "c2");
    const json* b1 = row_for(out["results"], "b1");
    REQUIRE(c1 != nullptr);
    REQUIRE(c2 != nullptr);
    REQUIRE(b1 != nullptr);
    check_rel((*c1)["axial_n"].get<double>(), -44397.63078423692);
    check_rel((*c1)["shear_n"].get<double>(), -9877.348508790408);
    check_rel((*c1)["moment_nmm"].get<double>(), 22187369.094084427);
    check_rel((*c1)["deflection_mm"].get<double>(), -56.13144429698194);
    check_rel((*c2)["axial_n"].get<double>(), -44402.36921576308);
    check_rel((*c2)["moment_nmm"].get<double>(), -22201584.388662644);
    check_rel((*b1)["axial_n"].get<double>(), -12277.348508790426);
    check_rel((*b1)["shear_n"].get<double>(), -22202.36921576308);
    check_rel((*b1)["moment_nmm"].get<double>(), 22201584.38866265);
    check_rel((*b1)["deflection_mm"].get<double>(), -94486.82297326467);
}

TEST_CASE("multi-combo run (list param)") {
    const json out = rpc("struct_analyze", {{"doc", load_fixture("portal.json")},
                                            {"load_combo", json::array({"DEAD_ONLY", "DEAD_LIVE"})}});
    CHECK(out["results"].size() == 10);  // 5 members × 2 combos
}

// ---------------------------------------------------------------------------
// D/C checks (mirrors test_checks.py + the oracle check rows)
// ---------------------------------------------------------------------------

TEST_CASE("check_member steel LRFD hand-calc (W8x31 A992 DoD case)") {
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
}

TEST_CASE("check_member wood NDS ASD") {
    const auto section = get_lumber_section("2x4");
    REQUIRE(section.has_value());
    const json row = {{"element_id", "j1"}, {"combo", "DEAD_LIVE"},
                      {"axial_n", -9000.0}, {"moment_nmm", 100000.0}};
    const CheckResult cr = check_member(row, *section, "df_no2");
    CHECK(cr.dc_axial == doctest::Approx(9000.0 / (9.307 * 3382.0)));
    CHECK(cr.dc_bending == doctest::Approx(100000.0 / (6.205 * 50250.0)));
    CHECK(cr.governing_limit == "bending_ASD_NDS");
}

TEST_CASE("struct_check portal (oracle parity, fallback + lumber sections)") {
    const json out = rpc("struct_check",
                         {{"doc", load_fixture("portal.json")}, {"load_combo", "DEAD_LIVE"}});
    CHECK(out["disclaimer"] == kCheckDisclaimer);
    const json* p1 = row_for(out["results"], "p1");
    const json* b1 = row_for(out["results"], "b1");
    const json* f1 = row_for(out["results"], "f1");
    REQUIRE(p1 != nullptr);
    REQUIRE(b1 != nullptr);
    REQUIRE(f1 != nullptr);
    check_rel((*p1)["dc_ratio"].get<double>(), 2.7256689359674233, 1e-9);
    check_rel((*p1)["dc_axial"].get<double>(), 0.1258760733348805, 1e-9);
    CHECK((*p1)["governing_limit"] == "bending_ASD_NDS");
    CHECK((*p1)["passed"] == false);
    check_rel((*b1)["dc_ratio"].get<double>(), 1.3988139501356378, 1e-9);
    // Concrete footing: not in either table → section_properties fallback row.
    CHECK((*f1)["dc_ratio"].get<double>() == 0.0);
    CHECK((*f1)["passed"] == true);
}

TEST_CASE("struct_check steel frame (AISC table sections)") {
    const json out = rpc("struct_check", {{"doc", load_fixture("steel_frame.json")}});
    const json* c1 = row_for(out["results"], "c1");
    const json* b1 = row_for(out["results"], "b1");
    REQUIRE(c1 != nullptr);
    REQUIRE(b1 != nullptr);
    check_rel((*c1)["dc_ratio"].get<double>(), 0.10381857342668512, 1e-9);
    CHECK((*c1)["governing_limit"] == "bending_yield_LRFD");
    check_rel((*b1)["dc_ratio"].get<double>(), 0.15877054307396818, 1e-9);
    check_rel((*b1)["dc_axial"].get<double>(), 0.006068472141001568, 1e-9);
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
    CHECK_THROWS_AS(steel_weight_lbs(bad), std::out_of_range);
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

// ---------------------------------------------------------------------------
// error envelopes
// ---------------------------------------------------------------------------

TEST_CASE("error envelopes") {
    json result;
    std::string err;
    // Unknown method falls through (integrator routes to the next layer).
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
}
