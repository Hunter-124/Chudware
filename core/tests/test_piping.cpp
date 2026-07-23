// core/tests/test_piping.cpp — piping core (OCCT-free, offline): fluid
// properties, schema, catalog, 1-D liquid/gas network solver, pump curves,
// operating point + NPSH, pressure-drop report, CFD handoff math, and the
// dispatch envelopes. Mirrors the Python oracle suites
// engine_oracle/piping/{test_properties,test_schema,test_catalog,test_solve,
// test_cfd_handoff}.py; the network-solve numbers are the same fixtures, so
// these cases double as the unit half of the T-RF19 parity bar (the
// byte-identical envelope diff lives in the /tmp harness driving rpc_cli).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#define _USE_MATH_DEFINES
#include <algorithm>
#include <cmath>
#include <map>
#include <vector>

#include "piping/catalog.h"
#include "piping/cfd.h"
#include "piping/fluid_properties.h"
#include "piping/piping.h"
#include "piping/schema.h"
#include "piping/solve.h"

using namespace aicad::piping;
using aicad::pyutil::ojson;

static ojson single_pipe_doc() {
    ojson doc = new_piping("case1");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[10000,0,0]]"), 2, "40"));
    return doc;
}

// ── fluid properties (test_properties.py) ────────────────────────────────────

TEST_CASE("properties: water reference point @20 °C") {
    const ojson r = fluid_properties("water", 293.15, P_ATM);
    REQUIRE(r["ok"] == true);
    CHECK(r["phase"] == "liquid");
    CHECK(std::abs(r["rho"].get<double>() - 998.2) < 0.1);  // NIST/IAPWS @20 °C
    CHECK(std::abs(r["mu"].get<double>() - 1.002e-3) < 5e-6);
    CHECK(std::abs(r["pvap"].get<double>() - 2339.0) < 5.0);  // saturation @20 °C
    CHECK(r["source"].get<std::string>().find("IAPWS") != std::string::npos);
    CHECK(r["backend"] == "builtin");
}

TEST_CASE("properties: water out of range soft-fails") {
    const ojson r = fluid_properties("water", 450.0);  // 176 °C — past the table
    CHECK(r["ok"] == false);
    CHECK(r["reason"].get<std::string>().find("range") != std::string::npos);
}

TEST_CASE("properties: air ideal gas + pressure scaling") {
    const ojson a = fluid_properties("air", 288.15, P_ATM);
    REQUIRE(a["ok"] == true);
    CHECK(a["phase"] == "gas");
    const double hand = P_ATM * 0.0289647 / (R_UNIVERSAL * 288.15);
    CHECK(std::abs(a["rho"].get<double>() - hand) < 1e-6);
    CHECK(std::abs(a["rho"].get<double>() - 1.225) < 0.005);
    CHECK(std::abs(a["mu"].get<double>() - 1.81e-5) < 5e-7);  // Sutherland @20 °C
    const ojson a2 = fluid_properties("air", 288.15, 2 * P_ATM);
    CHECK(std::abs(a2["rho"].get<double>() - 2 * a["rho"].get<double>()) < 1e-6);
}

TEST_CASE("properties: Sutherland viscosity rises with T; gases have molar mass") {
    const double lo = fluid_properties("nitrogen", 250.0)["mu"].get<double>();
    const double hi = fluid_properties("nitrogen", 400.0)["mu"].get<double>();
    CHECK(hi > lo);
    for (const char* name : {"methane", "natural_gas", "nitrogen", "carbon_dioxide"}) {
        const ojson r = fluid_properties(name, 288.15, 3e6);
        CHECK(r["ok"] == true);
        CHECK(r["molar_mass"].get<double>() > 0);
        CHECK(r["rho"].get<double>() > 0);
        CHECK(!r["source"].get<std::string>().empty());
    }
}

TEST_CASE("properties: unknown fluid / bad state soft-fail") {
    CHECK(fluid_properties("unobtanium")["ok"] == false);
    CHECK(fluid_properties("unobtanium")["reason"] == "unknown fluid 'unobtanium'");
    CHECK(fluid_properties("air", -5.0)["ok"] == false);
    CHECK(fluid_properties("air", 293.15, -1.0)["ok"] == false);
}

TEST_CASE("properties: resolve_solver_fluid shapes") {
    const ojson d = resolve_solver_fluid(nullptr);
    CHECK(std::abs(d["rho"].get<double>() - 998.2) < 0.1);
    CHECK(d["name"] == "water");
    CHECK(!d["source"].get<std::string>().empty());
    const ojson e = resolve_solver_fluid(ojson::parse(R"({"rho": 850.0, "mu": 5e-3})"));
    CHECK(e["rho"] == 850.0);
    CHECK(e["mu"] == 5e-3);
    const ojson g = resolve_solver_fluid(
        ojson::parse(R"({"name": "methane", "T": 288.15, "P": 3e6})"));
    CHECK(g["phase"] == "gas");
    CHECK(g["molar_mass"].get<double>() > 0);
    const ojson c = resolve_solver_fluid(ojson::parse(R"({"name": "water", "T_C": 20.0})"));
    CHECK(std::abs(c["rho"].get<double>() - 998.2) < 0.1);
    CHECK(resolve_solver_fluid(ojson::parse(R"({"foo": 1})"))["ok"] == false);
}

// ── schema (test_schema.py) ──────────────────────────────────────────────────

TEST_CASE("schema: factories + next_id") {
    ojson doc = new_piping("P");
    CHECK(doc["schema"] == "aicad.piping/1");
    CHECK(doc["units"]["system"] == "MMGS");
    CHECK(doc["material"] == "A106-B");
    CHECK(next_id(doc, "r") == "r1");
    CHECK(next_id(doc, "ft") == "ft1");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[100,0,0]]"), 2, "40"));
    doc["runs"].push_back(
        new_pipe_run("r3", ojson::parse("[[0,0,0],[100,0,0]]"), 2, "40"));
    CHECK(next_id(doc, "r") == "r4");
    const ojson run = new_pipe_run("r1", ojson::parse("[[0,0,0],[1,2,3]]"), 2);
    CHECK(run["path"][1] == ojson::array({1.0, 2.0, 3.0}));  // coords floated
    CHECK(run["end_type"] == "BW");
    CHECK(run["bore_mm"].is_null());
}

TEST_CASE("schema: validators") {
    ojson doc = new_piping("V");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[1000,0,0]]"), 2, "40"));
    doc["runs"].push_back(
        new_pipe_run("r2", ojson::parse("[[1000,0,0],[1000,1000,0]]"), 2, "40"));
    CHECK(validate_piping(doc).empty());

    // bad schedule / material / end_type
    ojson bad = doc;
    bad["runs"][0]["schedule"] = "99";
    auto errs = validate_piping(bad);
    REQUIRE(errs.size() == 1);
    CHECK(errs[0] == "runs[0].schedule: '99' not in SCHEDULES");

    // short path
    ojson shortp = new_piping("S");
    shortp["runs"].push_back(new_pipe_run("r1", ojson::parse("[[0,0,0]]"), 2));
    errs = validate_piping(shortp);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("need >= 2 points") != std::string::npos;
    }) != errs.end());

    // duplicate id
    ojson dup = doc;
    dup["runs"].push_back(dup["runs"][0]);
    errs = validate_piping(dup);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("duplicate") != std::string::npos;
    }) != errs.end());

    // wrong schema envelope
    ojson wrong = new_piping("W");
    wrong["schema"] = "wrong/1";
    errs = validate_piping(wrong);
    REQUIRE(errs.size() == 1);
    CHECK(errs[0] == "schema: expected 'aicad.piping/1' got 'wrong/1'");

    // fitting enums
    ojson ftdoc = doc;
    ftdoc["fittings"].push_back(
        new_fitting("ft1", "elbow", 2, ojson::array({1000, 0, 0}), "40", "Fitting",
                    ojson::parse(R"({"angle": 30})")));
    errs = validate_piping(ftdoc);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("ELBOW_ANGLES") != std::string::npos;
    }) != errs.end());
}

// ── catalog (test_catalog.py) ────────────────────────────────────────────────

TEST_CASE("catalog: nearest_pipe schedule lookup + aliases") {
    ojson spec = nearest_pipe(2, "40");
    REQUIRE(spec["ok"] == true);
    CHECK(spec["nps"] == 2.0);
    CHECK(spec["outer_dia"] == doctest::Approx(60.3));
    CHECK(spec["inner_dia"] == doctest::Approx(52.48));
    CHECK(spec["wall"] == doctest::Approx(3.91));
    // STD aliases to 40
    spec = nearest_pipe(2, "STD");
    CHECK(spec["inner_dia"] == doctest::Approx(52.48));
    CHECK(spec["schedule"] == "STD");
    // outer-diameter lookup
    spec = nearest_pipe(nullptr, "40", 60.3);
    CHECK(spec["nps"] == 2.0);
    // unknown size / schedule degrade contract-shaped
    const ojson bad = nearest_pipe(7.25, "40");
    CHECK(bad["ok"] == false);
    CHECK(bad["reason"].get<std::string>().find("NPS not in list") !=
          std::string::npos);
    CHECK(nearest_pipe(2, "bogus")["ok"] == false);
    CHECK(nearest_pipe()["ok"] == false);
}

TEST_CASE("catalog: fitting dims from the bundled CSV") {
    const ojson dims = fitting_dims("elbow", 2);
    CHECK(dims["center_to_end"] == doctest::Approx(76.2));
    CHECK(dims["length"].is_null());
    const ojson red = fitting_dims("reducer", 2, 1.0);
    CHECK(red["length"] == doctest::Approx(76.2));
    const ojson fl = fitting_dims("flange", 2, 150);
    CHECK(fl["od"] == doctest::Approx(152.4));
    CHECK(fl["bolt_holes"] == 4.0);
    const ojson miss = fitting_dims("elbow", 5);
    CHECK(miss["ok"] == false);
    CHECK(miss["hint"] == "nearest available nps=4.0");
    const auto sizes = available_sizes("elbow");
    CHECK(sizes.size() == 12);
    CHECK(sizes.front() == 0.5);
    CHECK(sizes.back() == 12.0);
}

// ── liquid network solve (test_solve.py) ─────────────────────────────────────

TEST_CASE("solve: single pipe Darcy drop") {
    const ojson doc = single_pipe_doc();
    const ojson res = solve(doc, ojson::parse(
                                     R"([{"node":"n0","pressure":300},{"node":"n1","pressure":100}])"));
    REQUIRE(res["ok"] == true);
    REQUIRE(res["converged"] == true);
    CHECK(std::abs(res["links"][0]["dp_kpa"].get<double>() - 200.0) < 1.0);
    CHECK(res["links"][0]["flow_m3h"].get<double>() > 0);
    CHECK(std::abs(res["max_imbalance_m3h"].get<double>()) < 1e-3);
}

TEST_CASE("solve: series mass conservation + elbow K lands on one run") {
    ojson doc = new_piping("series");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[5000,0,0]]"), 2, "40"));
    doc["runs"].push_back(
        new_pipe_run("r2", ojson::parse("[[5000,0,0],[5000,5000,0]]"), 2, "40"));
    doc["fittings"].push_back(
        new_fitting("ft1", "elbow", 2, ojson::array({5000, 0, 0}), "40", "Fitting",
                    ojson::parse(R"({"angle": 90, "radius": "LR"})")));
    const ojson res = solve(doc, ojson::parse(
                                     R"([{"node":"n0","pressure":400},{"node":"n2","pressure":100}])"));
    REQUIRE(res["ok"] == true);
    REQUIRE(res["converged"] == true);
    std::map<std::string, ojson> links;
    for (const auto& l : res["links"]) links[l["id"].get<std::string>()] = l;
    CHECK(std::abs(links["r1"]["flow_m3h"].get<double>() -
                   links["r2"]["flow_m3h"].get<double>()) < 1e-2);
    std::vector<double> ks = {links["r1"]["k_total"].get<double>(),
                              links["r2"]["k_total"].get<double>()};
    std::sort(ks.begin(), ks.end());
    CHECK(ks[0] == 0.0);
    CHECK(ks[1] > 0.0);
}

TEST_CASE("solve: demand-driven, branch split, parallel loop, gravity riser") {
    {
        ojson doc = new_piping("demand");
        doc["runs"].push_back(
            new_pipe_run("r1", ojson::parse("[[0,0,0],[100000,0,0]]"), 4, "40"));
        const ojson res = solve(doc, ojson::parse(R"([{"node":"n0","pressure":500},
                                                      {"node":"n1","demand":50.0}])"));
        REQUIRE(res["converged"] == true);
        CHECK(std::abs(res["links"][0]["flow_m3h"].get<double>() - 50.0) < 0.1);
    }
    {
        ojson doc = new_piping("branch");
        doc["runs"].push_back(
            new_pipe_run("r1", ojson::parse("[[0,0,0],[10000,0,0]]"), 4, "40"));
        doc["runs"].push_back(
            new_pipe_run("r2", ojson::parse("[[10000,0,0],[20000,0,0]]"), 3, "40"));
        doc["runs"].push_back(
            new_pipe_run("r3", ojson::parse("[[10000,0,0],[10000,10000,0]]"), 3, "40"));
        const ojson res = solve(doc, ojson::parse(R"([{"node":"n0","pressure":600},
                                                      {"node":"n2","demand":30},
                                                      {"node":"n3","demand":20}])"));
        REQUIRE(res["converged"] == true);
        std::map<std::string, double> flows;
        for (const auto& l : res["links"])
            flows[l["id"].get<std::string>()] = l["flow_m3h"].get<double>();
        CHECK(std::abs(flows["r1"] - 50.0) < 0.1);
        CHECK(std::abs((flows["r2"] + flows["r3"]) - 50.0) < 0.1);
    }
    {
        ojson doc = new_piping("parallel");
        doc["runs"].push_back(
            new_pipe_run("r1", ojson::parse("[[0,0,0],[5000,0,0],[10000,0,0]]"), 2, "40"));
        doc["runs"].push_back(
            new_pipe_run("r2", ojson::parse("[[0,0,0],[5000,-3000,0],[10000,0,0]]"), 3, "40"));
        const ojson res = solve(doc, ojson::parse(R"([{"node":"n0","pressure":300},
                                                      {"node":"n1","demand":60}])"));
        REQUIRE(res["converged"] == true);
        CHECK(res["nodes"].size() == 2);
        std::map<std::string, double> flows;
        for (const auto& l : res["links"])
            flows[l["id"].get<std::string>()] = l["flow_m3h"].get<double>();
        CHECK(std::abs((flows["r1"] + flows["r2"]) - 60.0) < 0.1);
        CHECK(flows["r2"] > flows["r1"]);  // the larger run carries more flow
    }
    {
        ojson doc = new_piping("riser");
        doc["runs"].push_back(
            new_pipe_run("r1", ojson::parse("[[0,0,0],[0,0,20000]]"), 4, "40"));
        const ojson res = solve(doc, ojson::parse(R"([{"node":"n0","pressure":300},
                                                      {"node":"n1","demand":0.001}])"));
        REQUIRE(res["converged"] == true);
        // Top node (z=20000) pressure ~ 300 - rho*g*20 ~ 104 kPa.
        CHECK(std::abs(res["nodes"][1]["pressure_kpa"].get<double>() - 104.0) < 3.0);
    }
}

TEST_CASE("solve: pump curves, affinity laws, pump in network") {
    const std::string curve = "flow,head\n0,50\n50,45\n100,30\n150,5\n";
    const ojson p1 = load_pump_curve(curve);
    REQUIRE(p1["ok"] == true);
    CHECK(p1["shutoff_head"] == 50.0);
    CHECK(p1["max_flow"] == 150.0);
    const ojson p2 = load_pump_curve(curve, 0.5);
    CHECK(std::abs(p2["shutoff_head"].get<double>() - 12.5) < 1e-6);  // H' = H·0.5²
    CHECK(std::abs(p2["max_flow"].get<double>() - 75.0) < 1e-6);       // Q' = Q·0.5
    CHECK(load_pump_curve("")["ok"] == false);
    CHECK(load_pump_curve("x,y\n1,2\n3,4\n")["ok"] == false);

    ojson doc = new_piping("pumpnet");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[50000,0,0]]"), 3, "40"));
    const ojson res = solve(doc,
                            ojson::parse(R"([{"node":"n0","pressure":100},
                                             {"node":"n1","demand":40.0}])"),
                            nullptr,
                            ojson::parse(R"([{"run":"r1",
                                              "curve":"flow,head\n0,50\n50,45\n100,30\n150,5\n",
                                              "direction":"a_to_b"}])"));
    REQUIRE(res["converged"] == true);
    CHECK(std::abs(res["links"][0]["flow_m3h"].get<double>() - 40.0) < 0.5);
    CHECK(res["links"][0]["pump_head_m"].get<double>() > 0);
}

TEST_CASE("solve: soft failures are contract-shaped") {
    // (a) no fixed node -> under-constrained
    ojson a = solve(single_pipe_doc(), ojson::parse(R"([{"node":"n0","demand":10}])"));
    CHECK(a["ok"] == false);
    // (b) bad node reference
    ojson b = solve(single_pipe_doc(), ojson::parse(R"([{"node":"nX","pressure":100}])"));
    CHECK(b["ok"] == false);
    CHECK(b["reason"] ==
          "boundary reference {'node': 'nX', 'pressure': 100} did not resolve to a node");
    // (c) schema-invalid doc -> errors, before the solve
    ojson bad = new_piping("bad");
    bad["runs"].push_back(new_pipe_run("r1", ojson::parse("[[0,0,0]]"), 2, "40"));
    ojson c = solve(bad, ojson::parse(
                             R"([{"node":"n0","pressure":100},{"node":"n1","pressure":50}])"));
    CHECK(c["ok"] == false);
    CHECK(c.contains("errors"));
}

TEST_CASE("solve: laminar hose line follows Hagen-Poiseuille, not the square law") {
    // The 6 mm laminar-line regression from the oracle's module comment:
    // flow must land on the linear-law value, not 37% under it.
    ojson doc = new_piping("laminar");
    doc["runs"].push_back(new_pipe_run("r1", ojson::parse("[[0,0,0],[1000,0,0]]"), 0.25,
                                       "40", "Run", 6.0));
    const ojson res = solve(doc,
                            ojson::parse(R"([{"node":"n0","pressure":100},
                                             {"node":"n1","pressure":0}])"),
                            ojson::parse(R"({"rho": 870.0, "mu": 0.0278})"));
    REQUIRE(res["converged"] == true);
    const double q_m3s = res["links"][0]["flow_m3h"].get<double>() / 3600.0;
    // Hagen-Poiseuille: Q = ΔP·π·D⁴ / (128·μ·L)
    const double hand = 100000.0 * M_PI * std::pow(0.006, 4) / (128 * 0.0278 * 1.0);
    CHECK(std::abs(q_m3s - hand) / hand < 0.01);
    CHECK(res["links"][0]["reynolds"].get<double>() < 2100.0);
}

TEST_CASE("solve: per-segment pressure-drop report with K breakdown") {
    const ojson doc = single_pipe_doc();
    const ojson res = solve(doc, ojson::parse(
                                     R"([{"node":"n0","pressure":300},{"node":"n1","pressure":100}])"));
    const ojson rep = pressure_drop_report(doc, res);
    REQUIRE(rep["ok"] == true);
    CHECK(rep["total_dp_kpa"].get<double>() > 0);
    REQUIRE(rep["segments"].size() == 1);
    CHECK(rep["segments"][0]["breakdown"][0]["item"] == "pipe friction");
    // no result and no boundary -> contract-shaped
    CHECK(pressure_drop_report(doc)["ok"] == false);
}

// ── gas network solve (test_solve.py) ────────────────────────────────────────

TEST_CASE("solve gas: single pipe matches the Weymouth closed form") {
    ojson doc = new_piping("gas1");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[1000000,0,0]]"), 8, "40"));
    const ojson res = solve(doc,
                            ojson::parse(R"([{"node":"n0","pressure":3000},
                                             {"node":"n1","pressure":2900}])"),
                            ojson::parse(R"({"name":"methane","T":288.15,"P":3.0e6})"),
                            nullptr, "gas");
    REQUIRE(res["ok"] == true);
    REQUIRE(res["converged"] == true);
    CHECK(res["network_type"] == "gas");
    CHECK(std::abs(res["max_imbalance_kgh"].get<double>()) < 1e-3);
    const ojson lk = res["links"][0];
    // Hand calc: ṁ² = A²·M/(R·T)·(P1²−P2²)/(f·L/D) with the solver's own fd.
    const ojson spec = nearest_pipe(8, "40");
    const double D = spec["inner_dia"].get<double>() / 1000.0;
    const double A = M_PI * D * D / 4.0;
    const double M = 0.0160425, T = 288.15, Lm = 1000.0;
    const double P1 = 3000 * 1000 + 101325, P2 = 2900 * 1000 + 101325;
    const double fd = lk["friction_factor"].get<double>();
    const double mdot =
        std::sqrt(A * A * M / (R_UNIVERSAL * T) * (P1 * P1 - P2 * P2) / (fd * Lm / D));
    CHECK(std::abs(lk["mass_flow_kgh"].get<double>() - mdot * 3600.0) / (mdot * 3600.0) <
          0.01);
    CHECK(lk["std_flow_nm3h"].get<double>() > 0);
    CHECK(lk["mass_flow_kgh"].get<double>() > 0);
}

TEST_CASE("solve gas: series mass conservation + named-fluid requirement") {
    ojson doc = new_piping("gas2");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[500000,0,0]]"), 6, "40"));
    doc["runs"].push_back(
        new_pipe_run("r2", ojson::parse("[[500000,0,0],[1000000,0,0]]"), 6, "40"));
    const ojson res = solve(doc,
                            ojson::parse(R"([{"node":"n0","pressure":2000},
                                             {"node":"n2","pressure":1800}])"),
                            ojson::parse(R"({"name":"natural_gas","T":288.15,"P":2.0e6})"),
                            nullptr, "gas");
    REQUIRE(res["converged"] == true);
    std::map<std::string, double> flows;
    for (const auto& l : res["links"])
        flows[l["id"].get<std::string>()] = l["mass_flow_kgh"].get<double>();
    CHECK(std::abs(flows["r1"] - flows["r2"]) < 1e-3);
    const double mid = res["nodes"][1]["pressure_abs_kpa"].get<double>();
    CHECK(mid > 1800 + 101.325);
    CHECK(mid < 2000 + 101.325);

    // {rho,mu} has no molar mass -> gas solve cannot proceed
    ojson doc3 = new_piping("gas3");
    doc3["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[1000,0,0]]"), 2, "40"));
    const ojson r = solve(doc3,
                          ojson::parse(R"([{"node":"n0","pressure":100},
                                           {"node":"n1","pressure":50}])"),
                          ojson::parse(R"({"rho":1.2,"mu":1.8e-5})"), nullptr, "gas");
    CHECK(r["ok"] == false);
    CHECK(r["reason"].get<std::string>().find("compressible") != std::string::npos);
}

// ── operating point + NPSH (test_solve.py) ───────────────────────────────────

static const char* kPumpCsv = "flow,head\n0,50\n50,45\n100,30\n150,5\n";

TEST_CASE("operating point: duty point, NPSH margin, no-intersection, derived curve") {
    // System H = 20 + 0.002·Q² intersects the 50–100 pump segment at Q*≈85.078.
    ojson op = operating_point(kPumpCsv, ojson::parse(R"({"static_head_m":20.0,"k":0.002})"));
    REQUIRE(op["ok"] == true);
    CHECK(std::abs(op["duty_point"]["flow_m3h"].get<double>() - 85.078) < 0.05);
    CHECK(std::abs(op["duty_point"]["head_m"].get<double>() -
                   (20.0 + 0.002 * 85.078 * 85.078)) < 0.05);

    op = operating_point(kPumpCsv, ojson::parse(R"({"static_head_m":20.0,"k":0.002})"),
                         nullptr, nullptr, ojson::parse(R"({"name":"water","T":293.15})"),
                         nullptr, ojson("flow,npshr\n0,2\n50,2.5\n100,4\n150,7\n"),
                         ojson::parse(
                             R"({"pressure_kpa":50.0,"elevation_m":2.0,"friction_m":0.5})"));
    const ojson npsh = op["npsh"];
    const double hand_npsha =
        (101325.0 + 50000.0 - 2339.0) / (998.21 * 9.80665) + 2.0 - 0.5;
    CHECK(std::abs(npsh["npsha_m"].get<double>() - hand_npsha) < 0.05);
    CHECK(std::abs(npsh["npshr_m"].get<double>() - 3.55) < 0.1);
    CHECK(std::abs(npsh["margin_m"].get<double>() -
                   (npsh["npsha_m"].get<double>() - npsh["npshr_m"].get<double>())) < 2e-3);
    CHECK(npsh["pvap_source"].get<std::string>().find("NIST") != std::string::npos);

    // Static lift (60 m) above the pump shutoff head (50 m) -> no duty point.
    op = operating_point(kPumpCsv, ojson::parse(R"({"static_head_m":60.0,"k":0.001})"));
    CHECK(op["ok"] == false);
    CHECK(op["reason"].get<std::string>().find("shutoff") != std::string::npos);

    // Derived system curve from a network solve at the reference demand.
    ojson doc = new_piping("opnet");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[30000,0,0]]"), 3, "40"));
    op = operating_point(kPumpCsv, nullptr, doc,
                         ojson::parse(R"([{"node":"n0","head":5.0},
                                          {"node":"n1","demand":40.0}])"),
                         nullptr, 40.0);
    REQUIRE(op["ok"] == true);
    CHECK(op["duty_point"]["flow_m3h"].get<double>() > 0);
    CHECK(op["system_curve"].contains("derived_from"));
}

// ── CFD handoff (test_cfd_handoff.py) ────────────────────────────────────────

TEST_CASE("cfd: boundary extraction from a solve result") {
    const ojson doc = single_pipe_doc();
    const ojson res = solve(doc, ojson::parse(
                                     R"([{"node":"n0","pressure":300},{"node":"n1","pressure":100}])"));
    const ojson bc = cfd_boundary_from_solve(res);
    REQUIRE(bc["ok"] == true);
    CHECK(bc["inlet_pressure_kpa"].get<double>() == doctest::Approx(300.0).epsilon(0.01));
    CHECK(bc["outlet_pressure_kpa"].get<double>() == doctest::Approx(100.0).epsilon(0.01));
    CHECK(bc["pressure_drop_1d_kpa"].get<double>() == doctest::Approx(200.0).epsilon(0.01));
    CHECK(bc["inlet_velocity_ms"].get<double>() > 0);
    CHECK(bc["inlet_link"] == "r1");
    CHECK(bc["outlet_node"] == "n1");
    CHECK(cfd_boundary_from_solve(ojson::parse(R"({"ok": false})"))["ok"] == false);
}

TEST_CASE("cfd: verify 1D vs 3D pressure drop + velocity advisory") {
    const ojson doc = single_pipe_doc();
    const ojson res = solve(doc, ojson::parse(
                                     R"([{"node":"n0","pressure":300},{"node":"n1","pressure":100}])"));
    const double dp = res["links"][0]["dp_kpa"].get<double>();
    // A CFD result whose drag-derived dP matches the 1D drop within tolerance.
    const double area_mm2 = M_PI / 4.0 * std::pow(res["links"][0]["id_mm"].get<double>(), 2);
    ojson cfd = ojson::object();
    cfd["ok"] = true;
    cfd["drag_n"] = dp * 1000.0 * (area_mm2 * 1e-6);
    cfd["frontal_area_mm2"] = area_mm2;
    ojson v = verify_pressure_drop(res, cfd, 20.0);
    REQUIRE(v["ok"] == true);
    CHECK(v["pass"] == true);
    CHECK(v["deviation_pct"].get<double>() < 20.0);
    CHECK(v["velocity_limits"]["checked"] == true);
    // 50% off -> FAIL with the deviation verdict
    cfd["drag_n"] = cfd["drag_n"].get<double>() * 1.5;
    v = verify_pressure_drop(res, cfd, 20.0);
    CHECK(v["pass"] == false);
    CHECK(v["verdict"].get<std::string>().rfind("FAIL:", 0) == 0);
    // Cd fallback when no drag/area
    ojson cfd2 = ojson::object(
        {{"ok", true}, {"cd", 0.5}, {"velocity_ms", 2.0}});
    v = verify_pressure_drop(res, cfd2, 20.0);
    CHECK(v["ok"] == true);
    CHECK(!v["warnings"].empty());
    // run_id-scoped comparison + unknown run
    CHECK(verify_pressure_drop(res, cfd, 20.0, "rZZ")["ok"] == false);
    CHECK(verify_pressure_drop(res, cfd, 20.0, "r1")["ok"] == true);
}

// ── dispatch envelopes ───────────────────────────────────────────────────────

TEST_CASE("dispatch: piping_solve envelope incl. node_menu; bad params") {
    ojson result;
    REQUIRE(call("piping_solve",
                 ojson::parse(R"({"doc": {"schema":"aicad.piping/1","id":"d1","name":"c",
                                           "units":{"system":"MMGS"},"material":"A106-B",
                                           "runs":[{"id":"r1","name":"Run","suppressed":false,
                                                    "path":[[0,0,0],[10000,0,0]],"nps":2,
                                                    "schedule":"40","material":null,
                                                    "bend_radius":null,"end_type":"BW",
                                                    "bore_mm":null}],
                                           "fittings":[]},
                                  "boundary":[{"node":"n0","pressure":300},
                                              {"node":"n1","pressure":100}]})"),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result.contains("node_menu"));
    CHECK(result["node_menu"].size() == 2);

    // missing doc / missing boundary / schema errors
    REQUIRE(call("piping_solve", ojson::object(), result));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "doc is required (directly or via source_handoff)");
    REQUIRE(call("piping_solve",
                 ojson::parse(
                     R"({"doc": {"schema":"aicad.piping/1","id":"d1","name":"c",
                                  "units":{"system":"MMGS"},"material":"A106-B",
                                  "runs":[{"id":"r1","name":"Run","suppressed":false,
                                           "path":[[0,0,0],[10000,0,0]],"nps":2,
                                           "schedule":"40","material":null,
                                           "bend_radius":null,"end_type":"BW",
                                           "bore_mm":null}],
                                  "fittings":[]}})"),
                 result));
    CHECK(result["reason"] == "boundary conditions are required");

    // unowned methods fall through
    CHECK(!call("doc_new", ojson::object(), result));
}

TEST_CASE("dispatch: piping_fluid + piping_report + verify_cfd envelopes") {
    ojson result;
    REQUIRE(call("piping_fluid", ojson::parse(R"({"name":"water","T":293.15,"P":101325.0})"),
                 result));
    CHECK(result["ok"] == true);
    CHECK(result["backend"] == "builtin");

    // report with a doc but no boundary
    const ojson doc = single_pipe_doc();
    REQUIRE(call("piping_report", ojson::object({{"doc", doc}}), result));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "boundary conditions are required to solve before reporting");
    REQUIRE(call("piping_report",
                 ojson::object(
                     {{"doc", doc},
                      {"boundary", ojson::parse(R"([{"node":"n0","pressure":300},
                                                    {"node":"n1","pressure":100}])")}}),
                 result));
    CHECK(result["ok"] == true);
    CHECK(result.contains("segments"));

    // verify_cfd requires inputs
    REQUIRE(call("piping_verify_cfd", ojson::object(), result));
    CHECK(result["ok"] == false);
    REQUIRE(call("piping_verify_cfd",
                 ojson::object({{"solve_result", ojson::object({{"ok", false}})}}), result));
    CHECK(result["reason"] == "cfd_result is required");
}
