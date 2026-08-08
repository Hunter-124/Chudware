// core/tests/test_hydraulic.cpp — hydraulic core (OCCT-free, offline): ISO
// 1219 circuit schema, cylinder/pump/accumulator/valve sizing hand-calcs,
// BOM, and the dispatch envelopes (incl. the hydraulic→piping and
// hydraulic→FEA handoff + verify flows end-to-end). Mirrors the Python
// oracle suite engine_oracle/hydraulic/test_hydraulic.py plus the
// services/mcp/test_hydraulic_handoff.py flow; byte-identical envelope parity
// is proven separately by the /tmp harness driving rpc_cli.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#define _USE_MATH_DEFINES
#include <cmath>
#include <limits>

#include "hydraulic/bom.h"
#include "hydraulic/handoff.h"
#include "hydraulic/hydraulic.h"
#include "hydraulic/schema.h"
#include "hydraulic/solve.h"
#include "piping/simhandoff.h"

using namespace chudware::hydraulic;
using chudware::pyutil::ojson;

// ── schema factories + validation (test_hydraulic.py) ────────────────────────

TEST_CASE("schema: new_hydraulic / new_component / new_hose / next_id") {
    const ojson doc = new_hydraulic("My Circuit");
    CHECK(doc["schema"] == "aicad.hydraulic/1");
    CHECK(doc["name"] == "My Circuit");
    CHECK(doc["units"]["pressure"] == "bar");
    CHECK(doc["units"]["flow"] == "L/min");
    CHECK(doc["units"]["force"] == "kN");
    CHECK(doc["units"]["position"] == "mm");
    CHECK(doc["id"].get<std::string>().size() == 36);  // uuid4
    CHECK(doc["components"].empty());
    CHECK(doc["hoses"].empty());
    CHECK(doc["annotations"].empty());

    const ojson c = new_component("c1", "pump", "Main Pump", 100, 200);
    CHECK(c["id"] == "c1");
    CHECK(c["type"] == "pump");
    CHECK(c["name"] == "Main Pump");
    CHECK(c["position"] == ojson::array({100.0, 200.0}));
    CHECK(c["angle"] == 0.0);
    CHECK(c["sourcing"]["mpn"].is_null());
    CHECK(c["sourcing"]["quantity"] == 1);
    CHECK(c["sourcing"]["unit"] == "ea");

    const ojson c2 = new_component("c1", "pump", "", 0, 0,
                                   ojson::parse(R"({"bore_mm": 80, "stroke_mm": 200})"));
    CHECK(c2["params"]["bore_mm"] == 80);
    CHECK(c2["params"]["stroke_mm"] == 200);
    CHECK(new_component("c1", "cylinder_linear")["name"] == "cylinder_linear");

    const ojson h = new_hose("h1", "PRESSURE_LINE",
                             ojson::parse(R"([{"component_id":"c1","port":"p1"},
                                              {"component_id":"c2","port":"p2"}])"));
    CHECK(h["connections"].size() == 2);

    ojson d2 = new_hydraulic();
    CHECK(next_id(d2, "c") == "c1");
    CHECK(next_id(d2, "h") == "h1");
    d2["components"].push_back(new_component("c1", "pump"));
    d2["components"].push_back(new_component("c3", "reservoir"));
    CHECK(next_id(d2, "c") == "c4");
}

static ojson minimal_doc() {
    ojson doc = new_hydraulic("Test");
    doc["components"].push_back(new_component("c1", "pump", "Pump", 0, 0));
    doc["components"].push_back(new_component("c2", "reservoir", "Tank", 100, 0));
    doc["hoses"].push_back(new_hose("h1", "PRESSURE",
                                    ojson::parse(R"([{"component_id":"c1","port":"p1"},
                                                     {"component_id":"c2","port":"p2"}])")));
    return doc;
}

TEST_CASE("schema: validate_hydraulic") {
    CHECK(validate_hydraulic(minimal_doc()).empty());

    ojson bad_type = minimal_doc();
    bad_type["components"][0]["type"] = "rocket_engine";
    auto errs = validate_hydraulic(bad_type);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("HYD_COMPONENT_TYPES") != std::string::npos;
    }) != errs.end());

    ojson bad_port = minimal_doc();
    bad_port["components"][0]["ports"] =
        ojson::parse(R"([{"id":"p1","port_type":"exhaust"}])");
    errs = validate_hydraulic(bad_port);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("HYD_PORT_TYPES") != std::string::npos;
    }) != errs.end());

    ojson dup = minimal_doc();
    dup["components"].push_back(new_component("c1", "filter"));
    errs = validate_hydraulic(dup);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("duplicate") != std::string::npos;
    }) != errs.end());

    ojson wrong = new_hydraulic();
    wrong["schema"] = "wrong/1";
    errs = validate_hydraulic(wrong);
    REQUIRE(errs.size() == 1);
    CHECK(errs[0].find("schema") != std::string::npos);

    ojson bad_ref = minimal_doc();
    bad_ref["hoses"].push_back(
        new_hose("h2", "DRAIN", ojson::parse(R"([{"component_id":"c999","port":"drain"}])")));
    errs = validate_hydraulic(bad_ref);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("c999") != std::string::npos;
    }) != errs.end());

    ojson bad_units = minimal_doc();
    bad_units["units"]["flow"] = "m3/s";
    CHECK_FALSE(validate_hydraulic(bad_units).empty());

    ojson one_ended = minimal_doc();
    one_ended["hoses"][0]["connections"].erase(1);
    CHECK_FALSE(validate_hydraulic(one_ended).empty());

    ojson duplicate_end = minimal_doc();
    duplicate_end["hoses"][0]["connections"][1] =
        duplicate_end["hoses"][0]["connections"][0];
    CHECK_FALSE(validate_hydraulic(duplicate_end).empty());

    ojson nonfinite = minimal_doc();
    nonfinite["components"][0]["position"][0] =
        std::numeric_limits<double>::infinity();
    CHECK_FALSE(validate_hydraulic(nonfinite).empty());
}

// ── sizing hand-calcs (test_hydraulic.py) ────────────────────────────────────

TEST_CASE("size_cylinder: 50 mm bore @200 bar → ≈39.27 kN; envelope guards") {
    const ojson r = size_cylinder(50, 0, 200, 100, 50.0, 1.0);
    REQUIRE(r["ok"] == true);
    CHECK(std::abs(r["extend_force_kn"].get<double>() - 39.270) < 0.01);

    CHECK(size_cylinder(80, 56, 200, 300)["retract_force_kn"].get<double>() <
          size_cylinder(80, 56, 200, 300)["extend_force_kn"].get<double>());
    CHECK(std::abs(size_cylinder(50, 0, 100, 500, 100)["cycle_time_s"].get<double>() - 5.0) <
          0.001);
    CHECK(size_cylinder(63, 0, 150, 200)["flow_extend_lpm"].get<double>() > 0);
    CHECK(std::abs(size_cylinder(100, 0, 200, 500)["area_bore_cm2"].get<double>() - 78.54) <
          0.1);
    // guards
    CHECK(size_cylinder(0, 0, 100, 100)["ok"] == false);
    CHECK(size_cylinder(0, 0, 100, 100)["reason"] == "bore_mm must be > 0");
    CHECK(size_cylinder(50, 50, 100, 100)["ok"] == false);
    CHECK(size_cylinder(50, 0, 100, 100, 50.0, 0.0)["ok"] == false);
    const double area_m2 = r["area_bore_cm2"].get<double>() * 1e-4;
    CHECK(r["extend_force_kn"].get<double>() ==
          doctest::Approx(area_m2 * 200.0e5 / 1000.0).epsilon(2e-5));
    CHECK(r["flow_extend_lpm"].get<double>() ==
          doctest::Approx(area_m2 * 0.05 * 60000.0).epsilon(2e-5));
    CHECK(size_cylinder(std::numeric_limits<double>::infinity(), 0, 100, 100)["ok"] ==
          false);
    CHECK(size_cylinder(50, 0, std::numeric_limits<double>::quiet_NaN(), 100)["ok"] ==
          false);
}

TEST_CASE("size_pump: 10 L/min @200 bar η=0.85 → ≈3.33/3.92 kW; displacement") {
    const ojson r = size_pump(10, 200, 0.85);
    REQUIRE(r["ok"] == true);
    CHECK(std::abs(r["hydraulic_power_kw"].get<double>() - 3.333) < 0.01);
    CHECK(std::abs(r["shaft_power_kw"].get<double>() - 3.922) < 0.01);
    CHECK(std::abs(size_pump(15, 100, 0.85, 1500)["displacement_cc_rev"].get<double>() -
                   10.0) < 0.01);
    CHECK(size_pump(20, 200)["torque_nm"].get<double>() > 0);
    CHECK(size_pump(0, 100)["ok"] == false);
    CHECK(size_pump(10, 100, 0.0)["ok"] == false);
    CHECK(r["hydraulic_power_kw"].get<double>() ==
          doctest::Approx((10.0 / 60000.0) * 200.0e5 / 1000.0).epsilon(2e-4));
    CHECK(r["shaft_power_kw"].get<double>() ==
          doctest::Approx(r["hydraulic_power_kw"].get<double>() / 0.85).epsilon(2e-4));
    CHECK(size_pump(std::numeric_limits<double>::quiet_NaN(), 100)["ok"] == false);
}

TEST_CASE("size_accumulator: Boyle hand-calc + ordering guards") {
    const ojson r = size_accumulator(100, 250, 150, 5);
    REQUIRE(r["ok"] == true);
    CHECK(std::abs(r["total_volume_l"].get<double>() - 18.73) < 0.1);
    CHECK(std::abs(size_accumulator(100, 300, 150, 2)["pressure_ratio"].get<double>() - 3.0) <
          1e-3);
    CHECK(size_accumulator(150, 300, 150, 5)["ok"] == false);  // pre >= min
    CHECK(size_accumulator(100, 150, 200, 5)["ok"] == false);  // min >= max
    CHECK(size_accumulator(100, 300, 150,
                           std::numeric_limits<double>::infinity())["ok"] == false);
}

TEST_CASE("valve_flow: Kv conversion + guards") {
    CHECK(valve_flow(10, 5)["flow_lpm"].get<double>() > 0);
    CHECK(std::abs(valve_flow(10, 1, 1.0)["Kv"].get<double>() - 8.65) < 0.001);
    CHECK(valve_flow(0, 5)["ok"] == false);
    CHECK(valve_flow(10, 0)["ok"] == false);
    CHECK(valve_flow(5, 8)["flow_lpm"].get<double>() >
          valve_flow(5, 2)["flow_lpm"].get<double>());
    CHECK(valve_flow(5, std::numeric_limits<double>::infinity())["ok"] == false);
}

TEST_CASE("fmu_simulate: always the soft-fail skipped envelope (no FMI runtime)") {
    const ojson r = fmu_simulate(new_hydraulic("FMU test"));
    CHECK(r["ok"] == false);
    CHECK(r["skipped"] == true);
    CHECK(r["reason"] == "fmpy not installed");
    CHECK(r.contains("hint"));
}

// ── BOM helpers (test_hydraulic.py) ──────────────────────────────────────────

static ojson bom_circuit() {
    ojson doc = new_hydraulic("BOM test");
    ojson c1 = new_component("c1", "pump", "Main Pump");
    c1["sourcing"]["mpn"] = "HYD-PUMP-001";
    c1["sourcing"]["manufacturer"] = "Bosch Rexroth";
    c1["sourcing"]["description"] = "Axial piston pump 18 cc/rev";
    c1["sourcing"]["category"] = "Hydraulic pump";
    doc["components"].push_back(c1);
    doc["components"].push_back(new_component("c2", "cylinder_linear", "Lift Cylinder"));
    ojson c3 = new_component("c3", "reservoir", "Tank");
    c3["sourcing"]["mpn"] = "TANK-020L";
    doc["components"].push_back(c3);
    return doc;
}

TEST_CASE("bom: lines, sourcing summary, suppressed exclusion") {
    const ojson doc = bom_circuit();
    ojson lines = hydraulic_bom_lines(doc);
    REQUIRE(lines.size() == 3);
    for (const auto& line : lines) {
        for (const char* k : {"id", "type", "name", "mpn", "manufacturer", "description",
                              "quantity", "unit", "category"})
            CHECK(line.contains(k));
    }
    const ojson pump = lines[0];
    CHECK(pump["mpn"] == "HYD-PUMP-001");
    CHECK(pump["manufacturer"] == "Bosch Rexroth");

    const ojson summary = sourcing_summary(doc);
    CHECK(summary["total"] == 3);
    CHECK(summary["sourced"] == 2);
    CHECK(summary["missing_mpn"] == ojson::array({"c2"}));
    CHECK(summary["by_type"]["pump"] == 1);
    CHECK(summary["by_type"]["cylinder_linear"] == 1);
    CHECK(summary["by_type"]["reservoir"] == 1);

    ojson sup = doc;
    sup["components"][0]["suppressed"] = true;
    lines = hydraulic_bom_lines(sup);
    CHECK(lines.size() == 2);
    for (const auto& l : lines) CHECK(l["id"] != "c1");
}

// ── line-loss estimate + hoses→piping translation ────────────────────────────

TEST_CASE("estimate_line_dp: laminar + turbulent regimes") {
    // 12.7 mm hose, 25 L/min ISO VG32: Re ≈ 1307 → laminar.
    const ojson lam = estimate_line_dp(25, 12.7, 1000);
    REQUIRE(lam["ok"] == true);
    CHECK(lam["regime"] == "laminar");
    CHECK(lam["reynolds"].get<double>() < 2300);
    // Hagen-Poiseuille: ΔP = 128·μ·L·Q/(π·D⁴)
    const double hand =
        128 * 0.0278 * 1.0 * (25 / 60000.0) / (M_PI * std::pow(0.0127, 4)) / 1000.0;
    CHECK(std::abs(lam["dp_kpa"].get<double>() - hand) < 0.01);
    // thin water at high flow → turbulent (Swamee-Jain)
    const ojson turb = estimate_line_dp(200, 25, 5000, 998.2, 1.002e-3);
    CHECK(turb["regime"] == "turbulent (Swamee-Jain)");
    CHECK(estimate_line_dp(0, 12.7, 1000)["ok"] == false);
    CHECK(estimate_line_dp(25, 12.7, 1000,
                           std::numeric_limits<double>::quiet_NaN(), 0.0278)["ok"] == false);
    CHECK(estimate_line_dp(std::numeric_limits<double>::max(), 12.7, 1000)["ok"] ==
          false);
}

// ── dispatch envelopes ───────────────────────────────────────────────────────

TEST_CASE("dispatch: hydraulic_new / add_component / add_hose / bom envelopes") {
    ojson result;
    REQUIRE(call("hydraulic_new", ojson::object({{"name", "Press"}}), result));
    REQUIRE(result["ok"] == true);
    CHECK(result["doc"]["name"] == "Press");
    CHECK(result["hint"] == "Use hydraulic_add_component to add ISO 1219 components.");

    ojson doc = result["doc"];
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc},
                                {"type", "pump"},
                                {"name", "Main Pump"},
                                {"params", ojson::parse(R"({"displacement_cc_rev": 18})")},
                                {"sourcing", ojson::parse(R"({"mpn": "A10VSO18DFR"})")}}),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result["component_id"] == "c1");
    CHECK(result["doc_component_count"] == 1);
    CHECK(result["warnings"].empty());
    CHECK(result["hint"].is_null());
    doc = result["doc"];

    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc}, {"type", "cylinder_linear"}}), result));
    doc = result["doc"];
    CHECK(result["component_id"] == "c2");

    // unknown component type -> contract-shaped with the valid-types hint
    ojson bad;
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc}, {"type", "rocket_engine"}}), bad));
    CHECK(bad["ok"] == false);
    CHECK(bad["reason"] == "unknown component type 'rocket_engine'");
    CHECK(bad["hint"].get<std::string>().find("'accumulator'") != std::string::npos);

    // no doc -> session-less core reports it like the MCP layer would
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"type", "pump"}}), bad));
    CHECK(bad["reason"] == "no active hydraulic circuit");

    REQUIRE(call("hydraulic_add_hose",
                 ojson::object(
                     {{"doc", doc},
                      {"net_name", "PRESSURE_LINE"},
                      {"connections", ojson::parse(R"([{"component_id":"c1","port":"p1"},
                                                       {"component_id":"c2","port":"p1"}])")}}),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result["hose_id"] == "h1");
    CHECK(result["doc_hose_count"] == 1);
    doc = result["doc"];

    REQUIRE(call("hydraulic_add_hose",
                 ojson::object(
                     {{"doc", doc},
                      {"net_name", "BAD"},
                      {"connections", ojson::parse(R"([{"component_id":"c999","port":"p1"},
                                                       {"component_id":"c2","port":"p2"}])")}}),
                 bad));
    CHECK(bad["ok"] == false);

    REQUIRE(call("hydraulic_bom", ojson::object({{"doc", doc}}), result));
    REQUIRE(result["ok"] == true);
    CHECK(result["bom_lines"].size() == 2);
    CHECK(result["summary"]["sourced"] == 1);
    CHECK(result["warnings"] == ojson::array({"component c2 has no MPN"}));
    CHECK(result["hint"] == "1 component(s) missing MPN — add to sourcing fields.");
}

TEST_CASE("dispatch: size_cylinder / size_pump tool wrappers add hint + warnings") {
    ojson result;
    REQUIRE(call("hydraulic_size_cylinder",
                 ojson::parse(R"({"bore_mm":50,"rod_mm":0,"pressure_bar":200,
                                   "stroke_mm":100,"efficiency":1.0})"),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result.contains("warnings"));
    CHECK(result["hint"].get<std::string>().find("Pump must supply >= ") == 0);
    CHECK(result["hint"].get<std::string>().find(" at 200.0 bar.") != std::string::npos);
    REQUIRE(call("hydraulic_size_cylinder",
                 ojson::parse(R"({"bore_mm":0,"pressure_bar":100,"stroke_mm":100})"),
                 result));
    CHECK(result["ok"] == false);  // solver errors pass through unwrapped

    REQUIRE(call("hydraulic_size_pump",
                 ojson::parse(R"({"flow_lpm":10,"pressure_bar":200,"efficiency":0.85})"),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result["hint"].get<std::string>().find("Select a pump with >= ") == 0);

    CHECK(!call("piping_solve", ojson::object(), result));  // not ours
}

TEST_CASE("dispatch: hydraulic→piping handoff runs the live solve + verify") {
    // Pump -> cylinder circuit, one pressure-line hose.
    ojson result;
    REQUIRE(call("hydraulic_new", ojson::object({{"name", "Press"}}), result));
    ojson doc = result["doc"];
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc}, {"type", "pump"}}), result));
    doc = result["doc"];
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc}, {"type", "cylinder_linear"}}), result));
    doc = result["doc"];
    REQUIRE(call("hydraulic_add_hose",
                 ojson::object(
                     {{"doc", doc},
                      {"net_name", "PRESSURE_LINE"},
                      {"connections", ojson::parse(R"([{"component_id":"c1","port":"p1"},
                                                       {"component_id":"c2","port":"p1"}])")}}),
                 result));
    doc = result["doc"];

    REQUIRE(call("hydraulic_piping_handoff",
                 ojson::object({{"doc", doc},
                                {"supply_pressure_bar", 210.0},
                                {"flow_lpm", 25.0},
                                {"load_pressure_bar", 190.0}}),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result["stage"] == "solved");
    REQUIRE(result.contains("simhandoff"));
    const ojson env = result["simhandoff"];
    CHECK(env["coupling"] == "hydraulic_to_piping");
    CHECK(env["kind"] == "handoff");
    CHECK(chudware::piping::simhandoff::validate_envelope(env).empty());
    // one hose line: inline piping doc + boundary + estimate + solve agree
    CHECK(result["lines"].size() == 1);
    CHECK(result["piping_doc"]["runs"].size() == 1);
    CHECK(result["solve"]["converged"] == true);
    CHECK(result["verify"]["pass"] == true);  // laminar: estimate == solve
    REQUIRE(result["margin"]["line_loss_solved_bar"].is_null());
    REQUIRE(result["margin"]["per_line"].contains("r1"));
    CHECK(result["margin"]["per_line"]["r1"]["pump_margin_solved_bar"].get<double>() ==
          doctest::Approx(20.0).epsilon(0.02));
    CHECK(result["margin"]["per_line"]["r1"]["pump_margin_solved_pct"].get<double>() ==
          doctest::Approx(9.52).epsilon(0.01));

    // verify step consumes the handoff + solve and rebuilds the envelope
    ojson v;
    REQUIRE(call("hydraulic_verify_piping",
                 ojson::object({{"handoff", env},
                                {"solve_result", result["solve"]},
                                {"supply_pressure_bar", 210.0},
                                {"load_pressure_bar", 190.0}}),
                 v));
    REQUIRE(v["ok"] == true);
    CHECK(v["pass"] == true);
    CHECK(v["checks"].size() == 1);
    CHECK(v["simhandoff"]["kind"] == "verify");
    CHECK(v["simhandoff"]["coupling"] == "hydraulic_to_piping");
    CHECK(chudware::piping::simhandoff::validate_envelope(v["simhandoff"]).empty());
    CHECK(v["margin"]["per_line"]["r1"]["pump_margin_solved_bar"] ==
          result["margin"]["per_line"]["r1"]["pump_margin_solved_bar"]);

    // The generated hose runs are independent cases.  Choose headroom between
    // one line loss and the sum of two identical line losses: both real
    // per-line margins remain positive while the old fabricated series margin
    // would be negative.
    ojson add_second;
    REQUIRE(call("hydraulic_add_hose",
                 ojson::object(
                     {{"doc", doc},
                      {"net_name", "RETURN_LINE"},
                      {"connections", ojson::parse(R"([{"component_id":"c1","port":"p2"},
                                                       {"component_id":"c2","port":"p2"}])")}}),
                 add_second));
    ojson two_hose;
    REQUIRE(call("hydraulic_piping_handoff",
                 ojson::object({{"doc", add_second["doc"]},
                                {"supply_pressure_bar", 210.0},
                                {"flow_lpm", 25.0},
                                {"load_pressure_bar", 209.7}}),
                 two_hose));
    REQUIRE(two_hose["ok"] == true);
    REQUIRE(two_hose["margin"]["per_line"].size() == 2);
    CHECK(two_hose["margin"]["line_loss_solved_bar"].is_null());
    CHECK(two_hose["margin"]["pump_margin_solved_bar"].is_null());
    double independent_loss_sum_bar = 0.0;
    for (auto it = two_hose["margin"]["per_line"].begin();
         it != two_hose["margin"]["per_line"].end(); ++it) {
        independent_loss_sum_bar += it->at("line_loss_solved_bar").get<double>();
        CHECK(it->at("pump_margin_solved_bar").get<double>() > 0.0);
    }
    CHECK(210.0 - 209.7 - independent_loss_sum_bar < 0.0);

    ojson wrong_link = result["solve"];
    wrong_link["links"][0]["id"] = "unrelated_run";
    const auto [missing_env, missing_checks] =
        verify_piping_envelope(env, wrong_link, 30.0);
    REQUIRE(missing_checks.size() == 1);
    CHECK(missing_checks[0]["pass"] == false);
    CHECK(missing_env["pass"] == false);

    ojson not_converged = result["solve"];
    not_converged["converged"] = false;
    REQUIRE(call("hydraulic_verify_piping",
                 ojson::object({{"handoff", env}, {"solve_result", not_converged}}), v));
    CHECK(v["ok"] == false);

    ojson failed_handoff;
    REQUIRE(call(
        "hydraulic_piping_handoff",
        ojson::object(
            {{"doc", doc},
             {"supply_pressure_bar", 210.0},
             {"flow_lpm", 25.0},
             {"fluid", ojson::parse(
                           R"({"name":"air","T":293.15,"P":101325})")}}),
        failed_handoff));
    CHECK(failed_handoff["ok"] == false);
    CHECK(failed_handoff["stage"] == "solve_failed");
    CHECK(failed_handoff["solve"]["ok"] == false);

    // no hoses -> contract-shaped
    ojson empty_doc;
    REQUIRE(call("hydraulic_new", ojson::object(), empty_doc));
    REQUIRE(call("hydraulic_piping_handoff",
                 ojson::object({{"doc", empty_doc["doc"]},
                                {"supply_pressure_bar", 210.0},
                                {"flow_lpm", 25.0}}),
                 v));
    CHECK(v["ok"] == false);
    CHECK(v["reason"] == "hydraulic circuit has no hoses");
}

TEST_CASE("dispatch: hydraulic→FEA handoff + verify envelopes") {
    ojson result;
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":80,"rod_mm":56,"max_pressure_bar":250})"),
                 result));
    REQUIRE(result["ok"] == true);
    // π/4·0.08²·250e5 ≈ 125 663.7 N extend
    CHECK(result["extend_force_n"].get<double>() ==
          doctest::Approx(125663.7).epsilon(1e-4));
    CHECK(result["cases"] ==
          ojson::array({"extend_max_pressure", "retract_max_pressure"}));
    CHECK(result["fea_args"]["extend_max_pressure"]["loads"][0]["force"].size() == 3);
    const ojson env = result["simhandoff"];
    CHECK(env["coupling"] == "hydraulic_to_fea");
    CHECK(chudware::piping::simhandoff::validate_envelope(env).empty());
    CHECK(env["conditions"].size() == 3);  // extend + retract + fixture

    // verify: the envelope's own conditions match the independent recompute
    ojson v;
    REQUIRE(call("hydraulic_verify_fea", ojson::object({{"handoff", env}}), v));
    REQUIRE(v["ok"] == true);
    CHECK(v["pass"] == true);
    CHECK(v["checks"].size() == 2);
    CHECK(v["simhandoff"]["kind"] == "verify");
    CHECK(chudware::piping::simhandoff::validate_envelope(v["simhandoff"]).empty());

    // a tampered condition (off-axis force) fails magnitude + flags colinearity
    ojson tampered = env;
    tampered["conditions"][0]["value"] = ojson::array({1000.0, 0.0, 0.0});
    REQUIRE(call("hydraulic_verify_fea", ojson::object({{"handoff", tampered}}), v));
    CHECK(v["pass"] == false);

    ojson missing_case = env;
    missing_case["conditions"].erase(1);
    CHECK(verify_fea_envelope(missing_case, 0.5)["pass"] == false);

    ojson duplicate_case = env;
    duplicate_case["conditions"].push_back(duplicate_case["conditions"][0]);
    CHECK(verify_fea_envelope(duplicate_case, 0.5)["pass"] == false);

    ojson missing_axis = env;
    missing_axis["meta"].erase("axis");
    CHECK(verify_fea_envelope(missing_axis, 0.5)["pass"] == false);

    // guards
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":50,"rod_mm":50,"max_pressure_bar":250})"),
                 v));
    CHECK(v["ok"] == false);
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":50,"max_pressure_bar":250,"axis":[0,0,0]})"),
                 v));
    CHECK(v["ok"] == false);
    CHECK(v["hint"] == "axis must be a non-zero [x,y,z] vector");
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":50,"max_pressure_bar":250,
                                   "load_face":"same","fixture_face":"same"})"),
                 v));
    CHECK(v["ok"] == false);
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":50,"max_pressure_bar":250,
                                   "target_doc_id":42})"),
                 v));
    CHECK(v["ok"] == false);
    REQUIRE(call("hydraulic_verify_fea", ojson::object(), v));
    CHECK(v["reason"] == "no hydraulic→FEA handoff envelope");
}
