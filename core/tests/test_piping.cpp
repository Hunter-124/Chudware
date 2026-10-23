// core/tests/test_piping.cpp — piping core (OCCT-free, offline): fluid
// properties, schema, catalog, 1-D liquid/gas network solver, pump curves,
// operating point + NPSH, pressure-drop report, CFD handoff math, and the
// dispatch envelopes. Mirrors the Python oracle suites
// engine_oracle/piping/{test_properties,test_schema,test_catalog,test_solve,
// test_cfd_handoff}.py; the network-solve numbers are the same fixtures, so
// these cases double as the unit half of the C++/Python parity bar.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#define _USE_MATH_DEFINES
#include <algorithm>
#include <array>
#include <cmath>
#include <map>
#include <limits>
#include <vector>

#include "piping/bom.h"
#include "piping/catalog.h"
#include "piping/cfd.h"
#include "piping/fluid_properties.h"
#include "piping/piping.h"
#include "piping/route.h"
#include "piping/schema.h"
#include "piping/solve.h"
#include "piping/spec.h"

using namespace chudware::piping;
using chudware::pyutil::ojson;

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
    CHECK(fluid_properties("air", std::numeric_limits<double>::quiet_NaN())["ok"] ==
          false);
    CHECK(fluid_properties("air", 293.15,
                           std::numeric_limits<double>::infinity())["ok"] == false);
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
    CHECK(resolve_solver_fluid(ojson::parse(R"({"rho":0.0,"mu":0.001})"))["ok"] ==
          false);
    CHECK(resolve_solver_fluid(ojson::parse(R"({"rho":998.0,"mu":-0.001})"))["ok"] ==
          false);
    CHECK(resolve_solver_fluid(ojson::parse(R"({"rho":998.0})"))["ok"] == false);
    ojson nonfinite_fluid = ojson::object(
        {{"rho", 998.0}, {"mu", std::numeric_limits<double>::infinity()}});
    CHECK(resolve_solver_fluid(nonfinite_fluid)["ok"] == false);
    CHECK(resolve_solver_fluid(
              ojson::parse(R"({"rho":998.0,"mu":0.001,"T_K":0.0})"))["ok"] == false);
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
                    ojson::parse(R"({"angle": 180})")));
    errs = validate_piping(ftdoc);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("ELBOW_ANGLES") != std::string::npos;
    }) != errs.end());

    ojson wrong_units = doc;
    wrong_units["units"]["system"] = "SI";
    CHECK_FALSE(validate_piping(wrong_units).empty());

    ojson nonfinite = doc;
    nonfinite["runs"][0]["path"][1][0] =
        std::numeric_limits<double>::quiet_NaN();
    CHECK_FALSE(validate_piping(nonfinite).empty());

    ojson bad_bore = doc;
    bad_bore["runs"][0]["bore_mm"] = std::numeric_limits<double>::infinity();
    CHECK_FALSE(validate_piping(bad_bore).empty());

    ojson closed = new_piping("closed");
    closed["runs"].push_back(new_pipe_run(
        "r1", ojson::parse("[[0,0,0],[1000,0,0],[0,0,0]]"), 2, "40"));
    CHECK_FALSE(validate_piping(closed).empty());
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
    const double p0 = res["nodes"][0]["pressure_kpa"].get<double>();
    const double p1 = res["nodes"][1]["pressure_kpa"].get<double>();
    CHECK(std::abs(std::abs(p0 - p1) -
                   std::abs(res["links"][0]["dp_kpa"].get<double>())) < 0.01);
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
    CHECK(load_pump_curve(curve, 0.0)["ok"] == false);
    CHECK(load_pump_curve("flow,head\n0,50\nnan,40\n")["ok"] == false);
    CHECK(load_pump_curve("flow,head\n0,50\n0,40\n")["ok"] == false);
    CHECK(load_pump_curve("flow,head\n0,40\n10,50\n")["ok"] == false);
    CHECK(load_pump_curve("flow,head\n0,50\n10,-1\n")["ok"] == false);

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

    // (d) every disconnected component needs its own fixed reference
    ojson disconnected = new_piping("disconnected");
    disconnected["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[1000,0,0]]"), 2, "40"));
    disconnected["runs"].push_back(
        new_pipe_run("r2", ojson::parse("[[10000,0,0],[11000,0,0]]"), 2, "40"));
    ojson d = solve(disconnected, ojson::parse(
        R"([{"node":"n0","pressure":100},{"node":"n1","demand":1}])"));
    CHECK(d["ok"] == false);
    CHECK(d["reason"].get<std::string>().find("disconnected") != std::string::npos);

    // (e) ambiguous/conflicting and non-finite boundaries are rejected
    CHECK(solve(single_pipe_doc(), ojson::parse(
              R"([{"node":"n0","pressure":100,"demand":1}])"))["ok"] == false);
    CHECK(solve(single_pipe_doc(), ojson::parse(
              R"([{"node":"n0","pressure":100},{"node":"n0","head":10}])"))["ok"] ==
          false);
    ojson nonfinite_boundary = ojson::array(
        {ojson::object({{"node", "n0"},
                        {"pressure", std::numeric_limits<double>::infinity()}})});
    CHECK(solve(single_pipe_doc(), nonfinite_boundary)["ok"] == false);

    // (f) unsupported catalog dimensions never silently invent a bore
    ojson overflowing_boundary = ojson::array(
        {ojson::object({{"node", "n0"},
                        {"pressure", std::numeric_limits<double>::max()}})});
    CHECK(solve(single_pipe_doc(), overflowing_boundary)["ok"] == false);
    ojson unsupported = new_piping("unsupported");
    unsupported["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[1000,0,0]]"), 7.25, "40"));
    CHECK(solve(unsupported, ojson::parse(
              R"([{"node":"n0","pressure":100},{"node":"n1","pressure":50}])"))["ok"] ==
          false);

    // (g) exhausting the iteration budget is an explicit failure, not success
    const ojson exhausted = solve(
        single_pipe_doc(),
        ojson::parse(R"([{"node":"n0","pressure":500},{"node":"n1","demand":50}])"),
        nullptr, nullptr, "liquid", 0.5, 1);
    CHECK(exhausted["ok"] == false);
    CHECK(exhausted["converged"] == false);
    CHECK(exhausted["iterations"] == 1);
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
    CHECK(rep["fluid"]["rho"].get<double>() ==
          doctest::Approx(res["fluid"]["rho"].get<double>()));
    ojson claimed = res;
    claimed["converged"] = false;
    CHECK(pressure_drop_report(doc, claimed)["ok"] == false);
    ojson missing_link = res;
    missing_link["links"] = ojson::array();
    CHECK(pressure_drop_report(doc, missing_link)["ok"] == false);

    const ojson oil = ojson::parse(
        R"({"name":"test_oil","rho":850.0,"mu":0.005,"source":"fixture"})");
    const ojson oil_res = solve(
        doc,
        ojson::parse(R"([{"node":"n0","pressure":300},{"node":"n1","pressure":100}])"),
        oil);
    REQUIRE(oil_res["ok"] == true);
    const ojson oil_rep = pressure_drop_report(doc, oil_res);
    REQUIRE(oil_rep["ok"] == true);
    CHECK(oil_rep["fluid"]["rho"] == 850.0);
    CHECK(oil_rep["fluid"]["source"] == "fixture");
}

TEST_CASE("pressure report: parallel branches are path totals, not a series sum") {
    ojson doc = new_piping("parallel-report");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[10000,0,0]]"), 2, "40"));
    doc["runs"].push_back(new_pipe_run(
        "r2", ojson::parse("[[0,0,0],[5000,5000,0],[10000,0,0]]"), 2, "40"));
    const ojson res = solve(
        doc, ojson::parse(
                 R"([{"node":"n0","pressure":300},{"node":"n1","demand":40}])"));
    REQUIRE(res["ok"] == true);
    REQUIRE(res["converged"] == true);

    const ojson rep = pressure_drop_report(doc, res);
    REQUIRE(rep["ok"] == true);
    REQUIRE(rep["path_analysis"]["complete"] == true);
    REQUIRE(rep["path_totals"].size() == 2);
    double segment_sum = 0.0;
    for (const auto& segment : rep["segments"])
        segment_sum += segment["dp_kpa"].get<double>();
    double governing = 0.0;
    for (const auto& path : rep["path_totals"])
        governing = std::max(governing, path["total_dp_kpa"].get<double>());
    CHECK(rep["total_dp_kpa"].get<double>() == doctest::Approx(governing));
    CHECK(rep["governing_path"]["total_dp_kpa"].get<double>() ==
          doctest::Approx(governing));
    CHECK(rep["governing_path"]["link_ids"].size() == 1);
    CHECK(governing < segment_sum * 0.75);
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
    ojson overflowing_gas_boundary = ojson::array(
        {ojson::object({{"node", "n0"},
                        {"pressure_abs", std::numeric_limits<double>::max()}})});
    CHECK(solve(doc3, overflowing_gas_boundary,
                ojson::parse(R"({"name":"methane","T":288.15,"P":3.0e6})"),
                nullptr, "gas")["ok"] == false);
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

    CHECK(operating_point(kPumpCsv,
                          ojson::parse(R"({"static_head_m":20.0,"k":-0.001})"))["ok"] ==
          false);
    CHECK(operating_point(kPumpCsv,
                          ojson::parse(R"({"static_head_m":20.0,"k":0.002})"),
                          nullptr, nullptr, nullptr, nullptr, nullptr, nullptr,
                          0.0)["ok"] == false);

    ojson bad_suction = operating_point(
        kPumpCsv, ojson::parse(R"({"static_head_m":20.0,"k":0.002})"),
        nullptr, nullptr, ojson::parse(R"({"name":"water","T":293.15})"),
        nullptr, ojson("flow,npshr\n0,2\n50,2.5\n100,4\n150,7\n"),
        ojson::parse(R"({"pressure_kpa":50.0,"friction_m":-1.0})"));
    CHECK(bad_suction["ok"] == false);

    ojson short_npshr = operating_point(
        kPumpCsv, ojson::parse(R"({"static_head_m":20.0,"k":0.002})"),
        nullptr, nullptr, ojson::parse(R"({"name":"water","T":293.15})"),
        nullptr, ojson("flow,npshr\n0,2\n50,2.5\n"),
        ojson::parse(R"({"pressure_kpa":50.0})"));
    CHECK(short_npshr["ok"] == false);

    ojson cavitating = operating_point(
        kPumpCsv, ojson::parse(R"({"static_head_m":20.0,"k":0.002})"),
        nullptr, nullptr, ojson::parse(R"({"name":"water","T":293.15})"),
        nullptr, ojson("flow,npshr\n0,2\n50,2.5\n100,4\n150,7\n"),
        ojson::parse(R"({"pressure_kpa":-100.0})"));
    REQUIRE(cavitating["ok"] == true);
    CHECK(cavitating["npsh"]["cavitation_risk"] == true);
    CHECK_FALSE(cavitating["warnings"].empty());

    // A demand boundary does not define its zero-flow terminal pressure.
    ojson doc = new_piping("opnet");
    doc["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[30000,0,0]]"), 3, "40"));
    op = operating_point(kPumpCsv, nullptr, doc,
                         ojson::parse(R"([{"node":"n0","pressure":0.0},
                                          {"node":"n1","demand":40.0}])"),
                         nullptr, 40.0);
    CHECK(op["ok"] == false);
    CHECK(op["reason"].get<std::string>().find("terminal-pressure") !=
          std::string::npos);

    op = operating_point(kPumpCsv, nullptr, doc,
                         ojson::parse(R"([{"node":"n0","pressure":0.0},
                                          {"node":"n1","demand":40.0,
                                           "terminal_pressure_kpa":0.0}])"),
                         nullptr, 40.0);
    REQUIRE(op["ok"] == true);
    CHECK(op["duty_point"]["flow_m3h"].get<double>() > 0);
    CHECK(op["system_curve"].contains("derived_from"));

    // Atmospheric source to atmospheric terminal 10 m higher requires +10 m
    // static head.  Five metres of source total head reduces that requirement
    // to +5 m rather than increasing it.
    ojson lift = new_piping("lift");
    lift["runs"].push_back(
        new_pipe_run("r1", ojson::parse("[[0,0,0],[0,0,10000]]"), 8, "40"));
    op = operating_point(kPumpCsv, nullptr, lift,
                         ojson::parse(R"([{"node":"n0","head":0.0},
                                          {"node":"n1","demand":40.0,
                                           "terminal_pressure_kpa":0.0}])"),
                         nullptr, 40.0);
    REQUIRE(op["ok"] == true);
    CHECK(op["system_curve"]["static_head_m"].get<double>() ==
          doctest::Approx(10.0));
    op = operating_point(kPumpCsv, nullptr, lift,
                         ojson::parse(R"([{"node":"n0","head":5.0},
                                          {"node":"n1","demand":40.0,
                                           "terminal_pressure_kpa":0.0}])"),
                         nullptr, 40.0);
    REQUIRE(op["ok"] == true);
    CHECK(op["system_curve"]["static_head_m"].get<double>() ==
          doctest::Approx(5.0));
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

TEST_CASE("cfd: reversed solved flow uses the signed upstream endpoint") {
    const ojson doc = single_pipe_doc();
    const ojson res = solve(
        doc, ojson::parse(
                 R"([{"node":"n0","pressure":100},{"node":"n1","pressure":300}])"));
    REQUIRE(res["ok"] == true);
    REQUIRE(res["links"][0]["flow_m3h"].get<double>() < 0.0);
    const ojson bc = cfd_boundary_from_solve(res, "r1");
    REQUIRE(bc["ok"] == true);
    CHECK(bc["flow_direction"] == "to_from");
    CHECK(bc["inlet_node"] == "n1");
    CHECK(bc["outlet_node"] == "n0");
    CHECK(bc["inlet_pressure_kpa"].get<double>() ==
          doctest::Approx(300.0).epsilon(0.01));
    CHECK(bc["outlet_pressure_kpa"].get<double>() ==
          doctest::Approx(100.0).epsilon(0.01));
    CHECK(bc["pressure_drop_1d_kpa"].get<double>() > 0.0);
    CHECK(bc["pressure_drop_1d_kpa"].get<double>() ==
          doctest::Approx(std::abs(res["links"][0]["dp_kpa"].get<double>())));
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

// ── routing, B31.3 spec check, BOM (contracts/piping_schema.md §8) ──────────

using Mat3 = std::array<std::array<double, 3>, 3>;
using P3 = std::array<double, 3>;

// build123d Rot(rx, ry, rz) (intrinsic XYZ) = Rx·Ry·Rz, degrees — the
// fitting `orient` convention of services/piping/threed.py _place.
static Mat3 rot_xyz(double ax, double ay, double az) {
    const double k = M_PI / 180.0;
    const double ca = std::cos(ax * k), sa = std::sin(ax * k);
    const double cb = std::cos(ay * k), sb = std::sin(ay * k);
    const double cc = std::cos(az * k), sc = std::sin(az * k);
    const Mat3 rx = {{{1, 0, 0}, {0, ca, -sa}, {0, sa, ca}}};
    const Mat3 ry = {{{cb, 0, sb}, {0, 1, 0}, {-sb, 0, cb}}};
    const Mat3 rz = {{{cc, -sc, 0}, {sc, cc, 0}, {0, 0, 1}}};
    auto mul = [](const Mat3& a, const Mat3& b) {
        Mat3 c{};
        for (int i = 0; i < 3; ++i)
            for (int j = 0; j < 3; ++j)
                for (int m = 0; m < 3; ++m) c[i][j] += a[i][m] * b[m][j];
        return c;
    };
    return mul(mul(rx, ry), rz);
}

static P3 pt3(const ojson& p) { return {p[0].get<double>(), p[1].get<double>(), p[2].get<double>()}; }

// Does the axis-aligned segment a-b enter the open box (lo, hi)?
static bool enters_open_box(const P3& a, const P3& b, const P3& lo, const P3& hi) {
    for (int k = 0; k < 3; ++k) {
        const double s0 = std::min(a[k], b[k]);
        const double s1 = std::max(a[k], b[k]);
        if (s1 <= lo[k] + 1e-6 || s0 >= hi[k] - 1e-6) return false;
    }
    return true;
}

TEST_CASE("route: orthogonal detour around a box with nozzle stubs, clearance and elbows") {
    const ojson params = ojson::parse(R"({
        "start": {"point": [0, 0, 0], "dir": [1, 0, 0]},
        "end": {"point": [1000, 0, 0], "dir": [1, 0, 0]},
        "nps": 2, "schedule": "40", "clearance_mm": 50,
        "obstacles": [{"min": [400, -200, -200], "max": [600, 200, 200], "name": "skid"}]})");
    ojson r;
    REQUIRE(call("piping_route", params, r));
    REQUIRE(r["ok"] == true);
    CHECK(validate_piping(r["piping"]).empty());

    // NPS 2 Sch 40: OD 60.3 → the centreline keeps 50 + 30.15 mm off the box.
    const double inflate = 50.0 + 60.3 / 2.0;
    const P3 lo = {400 - inflate, -200 - inflate, -200 - inflate};
    const P3 hi = {600 + inflate, 200 + inflate, 200 + inflate};
    const ojson& path = r["path"];
    // start, stub corner (rise), two corners over the box, drop corner, end
    REQUIRE(path.size() == 6);
    CHECK(path.front() == ojson::parse("[0.0, 0.0, 0.0]"));
    CHECK(path.back() == ojson::parse("[1000.0, 0.0, 0.0]"));
    double len = 0.0;
    for (size_t i = 0; i + 1 < path.size(); ++i) {
        const P3 a = pt3(path[i]);
        const P3 b = pt3(path[i + 1]);
        int moving = 0;
        for (int k = 0; k < 3; ++k) moving += std::abs(a[k] - b[k]) > 1e-9 ? 1 : 0;
        CHECK(moving == 1);  // orthogonal
        CHECK_FALSE(enters_open_box(a, b, lo, hi));
        len += std::abs(a[0] - b[0]) + std::abs(a[1] - b[1]) + std::abs(a[2] - b[2]);
    }
    CHECK(r["length_mm"].get<double>() == doctest::Approx(len).epsilon(1e-6));
    // Shortest detour hugs the clearance envelope: 1000 + 2·(200 + 80.15).
    CHECK(len == doctest::Approx(1000.0 + 2.0 * (200.0 + inflate)));
    // Leaves travelling +X past the B16.9 NPS 2 LR take-out (A = 76.2 mm)
    // and arrives travelling +X (end.dir) after the same take-out.
    CHECK(path[1][1].get<double>() == 0.0);
    CHECK(path[1][2].get<double>() == 0.0);
    CHECK(path[1][0].get<double>() >= 76.2 - 1e-6);
    CHECK(path[4][1].get<double>() == 0.0);
    CHECK(path[4][2].get<double>() == 0.0);
    CHECK(path[4][0].get<double>() <= 1000.0 - 76.2 + 1e-6);
    CHECK(r["bends"] == 4);
    CHECK(r["min_clearance_mm"].get<double>() >= 50.0 - 1e-6);
    CHECK(r["min_clearance_mm"].get<double>() == doctest::Approx(50.0));
    CHECK(r["nearest_obstacle"] == "skid");

    const ojson& run = r["piping"]["runs"][0];
    CHECK(run["id"] == "r1");
    CHECK(run["bend_radius"].get<double>() == doctest::Approx(76.2));
    const ojson& fts = r["piping"]["fittings"];
    REQUIRE(fts.size() == 4);
    CHECK(r["elbow_ids"] == ojson::parse(R"(["ft1","ft2","ft3","ft4"])"));
    for (size_t i = 1; i + 1 < path.size(); ++i) {
        const ojson& ft = fts[i - 1];
        CHECK(ft["type"] == "elbow");
        CHECK(ft["params"]["angle"] == 90);
        CHECK(ft["params"]["radius"] == "LR");
        CHECK(ft["origin"] == path[i]);
        // orient carries the canonical elbow (in +X, out +Y) onto the turn.
        const P3 a = pt3(path[i - 1]), b = pt3(path[i]), c = pt3(path[i + 1]);
        P3 din{}, dout{};
        const double l0 = std::abs(b[0] - a[0]) + std::abs(b[1] - a[1]) + std::abs(b[2] - a[2]);
        const double l1 = std::abs(c[0] - b[0]) + std::abs(c[1] - b[1]) + std::abs(c[2] - b[2]);
        for (int k = 0; k < 3; ++k) {
            din[k] = (b[k] - a[k]) / l0;
            dout[k] = (c[k] - b[k]) / l1;
        }
        const Mat3 R = rot_xyz(ft["orient"][0].get<double>(), ft["orient"][1].get<double>(),
                               ft["orient"][2].get<double>());
        for (int k = 0; k < 3; ++k) {
            CHECK(R[k][0] == doctest::Approx(din[k]));
            CHECK(R[k][1] == doctest::Approx(dout[k]));
        }
    }

    // A second route appends to the same document with fresh ids.
    ojson p2 = params;
    p2["piping"] = r["piping"];
    p2["start"]["point"] = ojson::array({0, 0, 1000});
    p2["end"]["point"] = ojson::array({1000, 0, 1000});
    ojson r2;
    REQUIRE(call("piping_route", p2, r2));
    REQUIRE(r2["ok"] == true);
    CHECK(r2["run_id"] == "r2");
    CHECK(r2["bends"] == 0);
    CHECK(r2["piping"]["runs"].size() == 2);
    CHECK(r2["piping"]["fittings"].size() == 4);
    CHECK(validate_piping(r2["piping"]).empty());
}

TEST_CASE("route: refusals — boxed-in start, start inside a solid, bad params") {
    // Six 50 mm slabs enclose the start: no orthogonal path can leave.
    ojson cage = ojson::array();
    for (int k = 0; k < 3; ++k) {
        for (int sgn : {1, -1}) {
            ojson lo = ojson::array({-350.0, -350.0, -350.0});
            ojson hi = ojson::array({350.0, 350.0, 350.0});
            if (sgn > 0) lo[k] = 300.0;
            else hi[k] = -300.0;
            cage.push_back({{"min", lo}, {"max", hi}});
        }
    }
    ojson params = ojson::object();
    params["start"] = {{"point", {0, 0, 0}}};
    params["end"] = {{"point", {2000, 0, 0}}};
    params["nps"] = 2;
    params["obstacles"] = cage;
    ojson r;
    REQUIRE(call("piping_route", params, r));
    CHECK(r["ok"] == false);
    CHECK(r["reason"] == "no_route");
    CHECK(r.contains("hint"));

    params["obstacles"] = ojson::parse(R"([{"min":[-10,-10,-10],"max":[10,10,10],"name":"tank"}])");
    REQUIRE(call("piping_route", params, r));
    CHECK(r["reason"] == "start_inside_obstacle");
    CHECK(r["hint"].get<std::string>().find("tank") != std::string::npos);
    // ...unless the body is ignored.
    params["ignore_bodies"] = ojson::array({"tank"});
    REQUIRE(call("piping_route", params, r));
    CHECK(r["ok"] == true);

    REQUIRE(call("piping_route", ojson::parse(R"({"start":{"point":[0,0,0]},
                                                 "end":{"point":[1,0,0]}})"),
                 r));
    CHECK(r["reason"] == "invalid_params");
}

static ojson spec_doc(const char* schedule) {
    ojson doc = new_piping("spec");
    doc["runs"].push_back(new_pipe_run("r1", ojson::parse("[[0,0,0],[6000,0,0]]"), 4, schedule));
    return doc;
}

TEST_CASE("spec_check: B31.3 eq. (3a) hand calc, MAWP, undersized schedule") {
    // NPS 4 Sch 40 A106-B (D = 114.3, t = 6.02 mm) at 2000 kPa / 100 °C.
    // Table A-1: S = 20.0 ksi (flat to 400 °F); E = W = 1, Y = 0.4,
    // c = 1.5 mm, mill tolerance 12.5 %.
    const double S = 20.0 * 6.894757293168361;  // 137.90 MPa
    const double P = 2.0;                        // MPa
    const double D = 114.3;
    const double t = P * D / (2.0 * (S + P * 0.4));       // 0.8241 mm
    const double t_req = (t + 1.5) / (1.0 - 0.125);       // 2.656 mm
    const double t_av = 6.02 * 0.875 - 1.5;               // 3.7675 mm
    const double mawp = 2.0 * S * t_av / (D - 2.0 * 0.4 * t_av) * 1000.0;  // ≈ 9337 kPa
    CHECK(t_req == doctest::Approx(2.656).epsilon(1e-3));

    ojson r;
    REQUIRE(call("piping_spec_check",
                 ojson::object({{"piping", spec_doc("40")},
                                {"design_pressure_kpa", 2000},
                                {"design_temp_c", 100}}),
                 r));
    REQUIRE(r["ok"] == true);
    CHECK(r["code"] == "ASME B31.3");
    const ojson& run = r["runs"][0];
    CHECK(std::abs(run["t_required_mm"].get<double>() - t_req) / t_req < 0.01);
    CHECK(run["allowable_mpa"].get<double>() == doctest::Approx(137.9).epsilon(1e-3));
    CHECK(run["mawp_kpa"].get<double>() == doctest::Approx(mawp).epsilon(1e-3));
    CHECK(run["pass"] == true);
    CHECK(r["all_pass"] == true);
    // Support span: positive, and no longer than the 2.5 mm-sag span.
    CHECK(run["support_span_m"].get<double>() > 3.0);
    CHECK(run["support_span_m"].get<double>() < 15.0);

    // 10 MPa exceeds the Sch 40 MAWP: fails, Sch 80 (8.56 mm) is suggested.
    REQUIRE(call("piping_spec_check",
                 ojson::object({{"piping", spec_doc("40")},
                                {"design_pressure_kpa", 10000},
                                {"design_temp_c", 100}}),
                 r));
    REQUIRE(r["ok"] == true);
    CHECK(r["runs"][0]["pass"] == false);
    CHECK(r["all_pass"] == false);
    CHECK(r["runs"][0]["suggested_schedule"] == "80");
    REQUIRE(call("piping_spec_check",
                 ojson::object({{"piping", spec_doc("80")},
                                {"design_pressure_kpa", 10000},
                                {"design_temp_c", 100}}),
                 r));
    CHECK(r["runs"][0]["pass"] == true);

    // Allowable stress interpolation (B31.3-2024 Table A-1C: 550 °F midway
    // 500 °F 19.0 / 600 °F 17.9 ksi → 19.0 + 0.5·(17.9 − 19.0) = 18.45 ksi) and
    // the stainless lower row (316 at 212 °F: 17.3 + 0.12·(15.6 − 17.3)).
    const ojson a = b31_3_allowable("A106-B", (550.0 - 32.0) / 1.8);
    CHECK(a["allowable_ksi"].get<double>() == doctest::Approx(18.45));
    const ojson s316 = b31_3_allowable("A312-316", 100.0);
    CHECK(s316["material"] == "A312-TP316");
    CHECK(s316["allowable_ksi"].get<double>() == doctest::Approx(17.096).epsilon(1e-4));
    CHECK(b31_3_allowable("A106-B", 500.0)["reason"] == "temperature_out_of_range");

    // Unknown material refuses by name.
    ojson pvc = spec_doc("40");
    pvc["material"] = "PVC";
    REQUIRE(call("piping_spec_check",
                 ojson::object({{"piping", pvc}, {"design_pressure_kpa", 500}}), r));
    CHECK(r["ok"] == false);
    CHECK(r["reason"] == "unknown_material");
    CHECK(r["hint"].get<std::string>().find("PVC") != std::string::npos);
}

TEST_CASE("spec_check: B16.5 flange rating — class 150 exceeded, class 300 passes") {
    // B16.5-2025 Table 2-1.1, group 1.1 at 100 °C: class 150 = 17.7 bar, class 300 = 46.7 bar.
    CHECK(b16_5_rating(150, "1.1", 100.0)["rating_kpa"].get<double>() == doctest::Approx(1770.0));
    // 75 °C midway 50 °C (19.1 bar) / 100 °C (17.7 bar): (19.1 + 17.7)/2 = 18.4 bar = 1840 kPa.
    CHECK(b16_5_rating(150, "1.1", 75.0)["rating_kpa"].get<double>() == doctest::Approx(1840.0));
    // Class 900 = class-600 row × 900/600: 102.0 bar × 1.5 = 153.0 bar = 15300 kPa.
    CHECK(b16_5_rating(900, "1.1", 38.0)["rating_kpa"].get<double>() ==
          doctest::Approx(102.0 * 1.5 * 100.0));
    CHECK(b16_5_rating(400, "1.1", 38.0)["reason"] == "unknown_flange_class");

    auto doc_with_flange = [](int cls) {
        ojson doc = spec_doc("40");
        doc["fittings"].push_back(new_fitting("ft1", "flange", 4, ojson::parse("[0,0,0]"), "40",
                                              "F1", ojson::object({{"flange_class", cls}})));
        return doc;
    };
    ojson r;
    REQUIRE(call("piping_spec_check",
                 ojson::object({{"piping", doc_with_flange(150)},
                                {"design_pressure_kpa", 2000},
                                {"design_temp_c", 100}}),
                 r));
    REQUIRE(r["ok"] == true);
    CHECK(r["runs"][0]["pass"] == true);
    REQUIRE(r["flanges"].size() == 1);
    CHECK(r["flanges"][0]["rating_kpa"].get<double>() == doctest::Approx(1770.0));
    CHECK(r["flanges"][0]["pass"] == false);
    CHECK(r["all_pass"] == false);
    REQUIRE(call("piping_spec_check",
                 ojson::object({{"piping", doc_with_flange(300)},
                                {"design_pressure_kpa", 2000},
                                {"design_temp_c", 100}}),
                 r));
    CHECK(r["flanges"][0]["pass"] == true);
    CHECK(r["all_pass"] == true);
}

static const ojson* find_item(const ojson& items, const char* kind) {
    for (const auto& it : items)
        if (it["kind"] == kind) return &it;
    return nullptr;
}

TEST_CASE("bom: take-outs, elbow/flange counts, welds and masses") {
    ojson doc = new_piping("bom");
    doc["runs"].push_back(new_pipe_run(
        "r1", ojson::parse("[[0,0,0],[2000,0,0],[2000,2000,0],[2000,2000,2000]]"), 4, "40"));
    const ojson elbow = ojson::object({{"angle", 90}, {"radius", "LR"}});
    doc["fittings"].push_back(new_fitting("ft1", "elbow", 4, ojson::parse("[2000,0,0]"), "40", "E1", elbow));
    doc["fittings"].push_back(new_fitting("ft2", "elbow", 4, ojson::parse("[2000,2000,0]"), "40", "E2", elbow));
    doc["fittings"].push_back(new_fitting("ft3", "flange", 4, ojson::parse("[0,0,0]"), "40", "F1",
                                          ojson::object({{"flange_class", 150}})));
    ojson r;
    REQUIRE(call("piping_bom", ojson::object({{"piping", doc}}), r));
    REQUIRE(r["ok"] == true);

    // Cut length = 6000 − 2 elbows × 2 × A (152.4, B16.9 NPS 4 LR) − flange 23.88.
    const ojson& ll = r["line_list"][0];
    CHECK(ll["length_mm"].get<double>() == doctest::Approx(6000.0));
    CHECK(ll["cut_length_mm"].get<double>() == doctest::Approx(6000.0 - 4 * 152.4 - 23.88).epsilon(1e-4));

    const ojson pipe = nearest_pipe(4, "40");
    const double aw = M_PI / 4.0 *
                      (std::pow(pipe["outer_dia"].get<double>(), 2) - std::pow(pipe["inner_dia"].get<double>(), 2));
    const ojson* p = find_item(r["items"], "pipe");
    const ojson* e = find_item(r["items"], "elbow");
    const ojson* f = find_item(r["items"], "flange");
    REQUIRE(p != nullptr);
    REQUIRE(e != nullptr);
    REQUIRE(f != nullptr);
    CHECK((*p)["unit_mass_kg"].get<double>() == doctest::Approx(aw * 1e-6 * 7850.0).epsilon(1e-3));  // ≈16.07 kg/m
    CHECK((*e)["qty"] == 2);
    // Pappus: wall annulus × R·θ (R = 152.4 mm, θ = π/2) × 7850 kg/m3.
    CHECK((*e)["unit_mass_kg"].get<double>() ==
          doctest::Approx(aw * 1e-6 * (152.4 * M_PI / 2.0 * 1e-3) * 7850.0).epsilon(1e-3));
    CHECK((*f)["qty"] == 1);
    CHECK((*f)["flange_class"] == 150);

    // 2 elbows × 2 butt welds + 1 weld-neck weld, all in the one spool.
    CHECK(r["totals"]["shop_welds"] == 5);
    CHECK(r["totals"]["field_welds"] == 0);
    REQUIRE(r["spools"].size() == 1);
    CHECK(r["spools"][0]["fitting_ids"].size() == 3);
    CHECK(r["spools"][0]["shop_welds"] == 5);
}

TEST_CASE("bom: spools split at max length and at flanged joints") {
    ojson doc = new_piping("long");
    doc["runs"].push_back(new_pipe_run("r1", ojson::parse("[[0,0,0],[30000,0,0]]"), 6, "40"));
    ojson r;
    REQUIRE(call("piping_bom", ojson::object({{"piping", doc}, {"max_spool_length_mm", 12000}}), r));
    REQUIRE(r["ok"] == true);
    REQUIRE(r["spools"].size() == 3);
    CHECK(r["spools"][0]["length_mm"].get<double>() == doctest::Approx(12000.0));
    CHECK(r["spools"][1]["length_mm"].get<double>() == doctest::Approx(12000.0));
    CHECK(r["spools"][2]["length_mm"].get<double>() == doctest::Approx(6000.0));
    for (const auto& s : r["spools"]) CHECK(s["run_ids"] == ojson::array({"r1"}));
    CHECK(r["totals"]["field_welds"] == 2);
    CHECK(r["totals"]["shop_welds"] == 0);
    CHECK(r["totals"]["pipe_length_m"].get<double>() == doctest::Approx(30.0));

    // Two runs bolted through a flange pair: two spools, one weld per flange.
    ojson fl = new_piping("flanged");
    fl["runs"].push_back(new_pipe_run("r1", ojson::parse("[[0,0,0],[3000,0,0]]"), 4, "40"));
    fl["runs"].push_back(new_pipe_run("r2", ojson::parse("[[3000,0,0],[6000,0,0]]"), 4, "40"));
    for (const char* id : {"ft1", "ft2"}) {
        fl["fittings"].push_back(new_fitting(id, "flange", 4, ojson::parse("[3000,0,0]"), "40", id,
                                             ojson::object({{"flange_class", 150}})));
    }
    REQUIRE(call("piping_bom", ojson::object({{"piping", fl}}), r));
    REQUIRE(r["ok"] == true);
    CHECK(r["spools"].size() == 2);
    CHECK(r["totals"]["flanged_joints"] == 1);
    CHECK(r["totals"]["shop_welds"] == 2);
    CHECK(r["totals"]["field_welds"] == 0);
    // Each run loses one flange thickness, not two.
    CHECK(r["line_list"][0]["cut_length_mm"].get<double>() == doctest::Approx(3000.0 - 23.88).epsilon(1e-4));
    CHECK(r["line_list"][1]["cut_length_mm"].get<double>() == doctest::Approx(3000.0 - 23.88).epsilon(1e-4));

    // Butt-welded joint without a flange keeps both runs in one spool.
    ojson bw = new_piping("welded");
    bw["runs"].push_back(new_pipe_run("r1", ojson::parse("[[0,0,0],[3000,0,0]]"), 4, "40"));
    bw["runs"].push_back(new_pipe_run("r2", ojson::parse("[[3000,0,0],[6000,0,0]]"), 4, "40"));
    REQUIRE(call("piping_bom", ojson::object({{"piping", bw}}), r));
    CHECK(r["spools"].size() == 1);
    CHECK(r["totals"]["shop_welds"] == 1);
}

TEST_CASE("dispatch: route/spec/bom/vessel/duct methods are owned by piping::call") {
    ojson result;
    for (const char* m : {"piping_route", "piping_spec_check", "piping_bom", "vessel_size", "duct_size"}) {
        CHECK(call(m, ojson::object(), result));
        CHECK(result["ok"] == false);
    }
}

TEST_CASE("edit: add_run on empty document creates an L run and a 90 degree elbow") {
    ojson result;
    REQUIRE(call("piping_edit", ojson::parse(R"({"ops":[{"op":"add_run",
        "path":[[0,0,0],[500,0,0],[1000,0,0],[1000,1000,0]]}]})"), result));
    REQUIRE(result["ok"] == true);
    const auto& doc = result["piping"];
    REQUIRE(doc["runs"].size() == 1);
    REQUIRE(doc["fittings"].size() == 1);
    CHECK(doc["runs"][0]["path"].size() == 3);
    CHECK(doc["fittings"][0]["type"] == "elbow");
    CHECK(doc["fittings"][0]["params"]["angle"].get<double>() == doctest::Approx(90));
    CHECK(doc["fittings"][0]["run_id"] == doc["runs"][0]["id"]);
    CHECK(validate_piping(doc).empty());
}

TEST_CASE("edit: mid-run branch splits a run adds a tee and conserves network mass") {
    ojson result;
    REQUIRE(call("piping_edit", ojson::parse(R"({"ops":[
        {"op":"add_run","path":[[0,0,0],[10000,0,0]]},
        {"op":"add_run","path":[[5000,0,0],[5000,5000,0]]}]})"), result));
    REQUIRE(result["ok"] == true);
    const auto doc = result["piping"];
    REQUIRE(doc["runs"].size() == 3);
    REQUIRE(doc["fittings"].size() == 1);
    const auto tee = doc["fittings"][0];
    CHECK(tee["type"] == "tee");
    REQUIRE(tee["params"]["run_ids"].size() == 2);
    const auto pair = tee["params"]["run_ids"];
    const auto branch = tee["params"]["branch_run"];
    CHECK(tee["run_id"] == branch);
    REQUIRE(call("piping_solve", {{"doc", doc}, {"boundary", ojson::parse(R"([
        {"at":[0,0,0],"pressure":300},{"at":[10000,0,0],"pressure":100},
        {"at":[5000,5000,0],"pressure":100}])")}}, result));
    REQUIRE(result["ok"] == true);
    std::map<std::string, double> flows;
    for (const auto& link : result["links"])
        flows[link["id"].get<std::string>()] = link["flow_m3h"].get<double>();
    CHECK(flows[pair[1].get<std::string>()] > 0);
    CHECK(flows[branch.get<std::string>()] > 0);
    CHECK(std::abs(flows[pair[0].get<std::string>()] -
                   flows[pair[1].get<std::string>()] - flows[branch.get<std::string>()]) < 1e-3);
    const auto up = run_loss_1d(nlohmann::json(doc), pair[0].get<std::string>(), 998.2, 0.001, 0.001, -1);
    const auto down = run_loss_1d(nlohmann::json(doc), pair[1].get<std::string>(), 998.2, 0.001, 0.001, -1);
    const auto side = run_loss_1d(nlohmann::json(doc), branch.get<std::string>(), 998.2, 0.001, 0.001, -1);
    REQUIRE(up["ok"] == true);
    REQUIRE(down["ok"] == true);
    REQUIRE(side["ok"] == true);
    CHECK(up["fittings"].empty());
    REQUIRE(down["fittings"].size() == 1);
    REQUIRE(side["fittings"].size() == 1);
    CHECK(side["fittings_K"].get<double>() == doctest::Approx(3 * down["fittings_K"].get<double>()));
}

TEST_CASE("fitting_k: gate valve at NPS 2 schedule 40 is eight Crane fT") {
    ojson result;
    REQUIRE(call("piping_fitting_k", ojson::parse(R"({"fitting":{
        "type":"valve","nps":2,"params":{"valve_type":"gate"}},"schedule":"40"})"), result));
    REQUIRE(result["ok"] == true);
    const double d = 0.05248;
    const double fT = 1 / std::pow(-2 * std::log10(0.0000457 / (3.7*d)), 2);
    CHECK(std::abs(result["K"].get<double>() / (8*fT) - 1) < 0.01);
    CHECK(result["fT"].get<double>() == doctest::Approx(fT));
    CHECK(result["inner_dia_mm"].get<double>() == doctest::Approx(52.48));
    CHECK(result["method"].get<std::string>().find("Crane") != std::string::npos);
    REQUIRE(result["curve"].size() == 24);
    CHECK(result["curve"].front()["re"].get<double>() == doctest::Approx(1e3));
    CHECK(result["curve"].back()["re"].get<double>() == doctest::Approx(1e7));
}

TEST_CASE("fitting_k: two_k three_k cv and log Reynolds table models") {
    ojson args = ojson::parse(R"({"fitting":{"type":"custom","nps":2,"params":{}},
                                 "reynolds":10000})");
    const double inches = 52.48 / 25.4;
    ojson result;
    args["fitting"]["params"]["k_model"] = {{"kind","two_k"},{"K1",1000},{"Kinf",0.5}};
    REQUIRE(call("piping_fitting_k", args, result));
    REQUIRE(result["ok"] == true);
    CHECK(result["K"].get<double>() == doctest::Approx(0.1 + 0.5*(1 + 1/inches)));
    args["fitting"]["params"]["k_model"] = {{"kind","three_k"},{"K1",1000},{"Ki",0.2},{"Kd",2}};
    REQUIRE(call("piping_fitting_k", args, result));
    REQUIRE(result["ok"] == true);
    CHECK(result["K"].get<double>() == doctest::Approx(0.1 + 0.2*(1 + 2/std::pow(inches,0.3))));
    args["fitting"]["params"]["k_model"] = {{"kind","cv"},{"Cv",20}};
    REQUIRE(call("piping_fitting_k", args, result));
    REQUIRE(result["ok"] == true);
    CHECK(result["K"].get<double>() == doctest::Approx(890.3*std::pow(inches,4)/400));
    args["fitting"]["params"]["k_model"] = ojson::parse(R"({"kind":"table","source":"cfd",
        "points":[{"re":1000,"K":4},{"re":100000,"K":2}]})");
    REQUIRE(call("piping_fitting_k", args, result));
    REQUIRE(result["ok"] == true);
    CHECK(result["K"].get<double>() == doctest::Approx(3));
    args["reynolds"] = 100;
    REQUIRE(call("piping_fitting_k", args, result));
    CHECK(result["K"].get<double>() == doctest::Approx(4));
    args["reynolds"] = 1e7;
    REQUIRE(call("piping_fitting_k", args, result));
    CHECK(result["K"].get<double>() == doctest::Approx(2));
    const auto doc = single_pipe_doc();
    REQUIRE(call("piping_edit", {{"piping",doc},{"ops",ojson::array({{
        {"op","add_fitting"},{"type","custom"},{"at",ojson::array({5000,0,0})},
        {"params",args["fitting"]["params"]}}})}}, result));
    REQUIRE(result["ok"] == true);
    const double flow = 10000 * 0.001 * M_PI * 0.05248 / (4 * 998.2);
    const auto loss = run_loss_1d(nlohmann::json(result["piping"]), "r1", 998.2, 0.001, flow, -1);
    REQUIRE(loss["ok"] == true);
    CHECK(loss["fittings_K"].get<double>() == doctest::Approx(3));
}

TEST_CASE("edit: custom fitting without k_model refuses invalid_k_model") {
    ojson result;
    REQUIRE(call("piping_edit", {{"piping",single_pipe_doc()},
        {"ops",ojson::parse(R"([{"op":"add_fitting","type":"custom","at":[5000,0,0]}])")}}, result));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "invalid_k_model");
    CHECK(result["op_index"] == 0);
    REQUIRE(call("piping_fitting_k", ojson::parse(R"({"fitting":{"type":"custom","params":{}}})"), result));
    CHECK(result["reason"] == "invalid_k_model");
}

TEST_CASE("edit: invalid second operation is atomic and reports op_index one") {
    const auto doc = single_pipe_doc();
    ojson params = {{"piping",doc},{"ops",ojson::parse(R"([
        {"op":"set_run","id":"r1","name":"Changed"},
        {"op":"split_run","run_id":"r999","point":[5000,0,0]}])")}};
    ojson result;
    REQUIRE(call("piping_edit", params, result));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "unknown_run");
    CHECK(result["op_index"] == 1);
    CHECK(params["piping"] == doc);
    CHECK_FALSE(result.contains("piping"));
}

TEST_CASE("edit: remove branch merges collinear split runs back") {
    ojson result;
    REQUIRE(call("piping_edit", ojson::parse(R"({"ops":[
        {"op":"add_run","path":[[0,0,0],[10000,0,0]]},
        {"op":"add_run","path":[[5000,0,0],[5000,5000,0]]}]})"), result));
    REQUIRE(result["ok"] == true);
    const auto doc = result["piping"];
    const auto branch = doc["fittings"][0]["params"]["branch_run"];
    REQUIRE(call("piping_edit", {{"piping",doc},{"ops",ojson::array({{
        {"op","remove_run"},{"id",branch}}})}}, result));
    REQUIRE(result["ok"] == true);
    REQUIRE(result["piping"]["runs"].size() == 1);
    CHECK(result["piping"]["fittings"].empty());
    CHECK(result["piping"]["runs"][0]["path"] == ojson::parse("[[0,0,0],[10000,0,0]]"));
    CHECK(result["removed"].size() == 3);
}

TEST_CASE("edit: moving a vertex keeps connected endpoint attached") {
    ojson result;
    REQUIRE(call("piping_edit", ojson::parse(R"({"ops":[
        {"op":"add_run","path":[[0,0,0],[1000,0,0]]},
        {"op":"add_run","path":[[1000,0,0],[1000,1000,0]]},
        {"op":"move_vertex","run_id":"r1","index":1,"point":[1200,100,0]}]})"), result));
    REQUIRE(result["ok"] == true);
    const auto& doc = result["piping"];
    CHECK(doc["runs"][0]["path"].back() == ojson::array({1200,100,0}));
    CHECK(doc["runs"][1]["path"].front() == doc["runs"][0]["path"].back());
    REQUIRE(doc["fittings"].size() == 1);
    CHECK(doc["fittings"][0]["origin"] == doc["runs"][0]["path"].back());
    CHECK(doc["fittings"][0]["params"]["angle"].get<double>() > 90);
}

TEST_CASE("edit: inline reducer splits run and attributes fitting to only one link") {
    ojson result;
    REQUIRE(call("piping_edit", {{"piping",single_pipe_doc()},
        {"ops",ojson::parse(R"([{"op":"add_fitting","type":"reducer","at":[5000,10,0],"nps2":1}])")}}, result));
    REQUIRE(result["ok"] == true);
    const auto& doc = result["piping"];
    REQUIRE(doc["runs"].size() == 2);
    CHECK(doc["runs"][1]["nps"] == 1);
    REQUIRE(doc["fittings"].size() == 1);
    CHECK(doc["fittings"][0]["run_id"] == "r1");
    CHECK(doc["fittings"][0]["origin"] == ojson::array({5000,0,0}));
    const auto a = run_loss_1d(nlohmann::json(doc),"r1",998.2,0.001,0.001,-1);
    const auto b = run_loss_1d(nlohmann::json(doc),"r2",998.2,0.001,0.001,-1);
    REQUIRE(a["ok"] == true);
    REQUIRE(b["ok"] == true);
    CHECK(a["fittings"].size() == 1);
    CHECK(b["fittings"].empty());
}

TEST_CASE("edit: reducer outlet update propagates downstream specification") {
    ojson result;
    REQUIRE(call("piping_edit", {{"piping",single_pipe_doc()},
        {"ops",ojson::parse(R"([
            {"op":"add_fitting","type":"reducer","at":[5000,0,0],"nps2":1},
            {"op":"add_fitting","type":"valve","run_id":"r2","at":[7500,0,0],"params":{"valve_type":"gate"}},
            {"op":"update_fitting","id":"ft1","params":{"nps2":1.5}}])")}}, result));
    REQUIRE(result["ok"] == true);
    const auto& doc = result["piping"];
    CHECK(doc["fittings"][0]["nps2"] == 1.5);
    CHECK_FALSE(doc["fittings"][0]["params"].contains("nps2"));
    CHECK(doc["runs"][1]["nps"] == 1.5);
    CHECK(doc["fittings"][1]["nps"] == 1.5);
}

TEST_CASE("edit: fourth run at a tee refuses node_full atomically") {
    ojson result;
    REQUIRE(call("piping_edit", ojson::parse(R"({"ops":[
        {"op":"add_run","path":[[0,0,0],[10000,0,0]]},
        {"op":"add_run","path":[[5000,0,0],[5000,5000,0]]}]})"), result));
    REQUIRE(result["ok"] == true);
    const auto doc = result["piping"];
    REQUIRE(call("piping_edit", {{"piping",doc},{"ops",ojson::parse(R"([
        {"op":"add_run","path":[[5000,0,0],[5000,-5000,0]]}])")}}, result));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "node_full");
    CHECK_FALSE(result.contains("piping"));
}

TEST_CASE("edit: updating elbow radius retains the shared solver correlation") {
    ojson result;
    REQUIRE(call("piping_edit", ojson::parse(R"({"ops":[
        {"op":"add_run","path":[[0,0,0],[1000,0,0],[1000,1000,0]]},
        {"op":"update_fitting","id":"ft1","params":{"radius":"SR"}}]})"), result));
    REQUIRE(result["ok"] == true);
    const auto doc = result["piping"];
    CHECK(doc["runs"][0]["bend_radius"].get<double>() == doctest::Approx(50.8));
    const auto loss = run_loss_1d(nlohmann::json(doc),"r1",998.2,0.001,0.001,-1);
    REQUIRE(loss["ok"] == true);
    REQUIRE(loss["fittings"].size() == 1);
    CHECK(loss["fittings"][0]["method"].get<std::string>().find("SR") != std::string::npos);
    REQUIRE(call("piping_fitting_k", {{"fitting",doc["fittings"][0]},
        {"reynolds",loss["reynolds"].get<double>()}}, result));
    REQUIRE(result["ok"] == true);
    // Preview uses clean-steel roughness 0.0457 mm, run uses material 0.046 mm.
    CHECK(std::abs(result["K"].get<double>() / loss["fittings_K"].get<double>() - 1) < 0.01);
}

TEST_CASE("fitting_k: invalid tables and orifice ratios refuse rather than falling back") {
    ojson result;
    for (const auto& model : ojson::parse(R"([
        {"kind":"constant","K":-1},
        {"kind":"cv","Cv":0},
        {"kind":"table","source":"user","points":[{"re":10000,"K":1},{"re":1000,"K":2}]},
        {"kind":"table","source":"user","points":[]},
        {"kind":"not_a_model"}
    ])")) {
        REQUIRE(call("piping_fitting_k", {{"fitting",{{"type","custom"},{"params",{{"k_model",model}}}}}}, result));
        CHECK(result["ok"] == false);
        CHECK(result["reason"] == "invalid_k_model");
    }
    REQUIRE(call("piping_fitting_k", ojson::parse(R"({"fitting":{"type":"orifice",
        "params":{"beta":2,"bore_mm":10}}})"), result));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "invalid_params");
}

// ── tee per-path K + strict fitting params (F2 cooling-water benchmark shape) ──

static ojson f2_network(ojson& tee_id, std::string& run_a, std::string& run_b) {
    ojson result;
    REQUIRE(call("piping_edit", ojson::parse(R"({"ops":[
        {"op":"add_run","path":[[0,0,0],[10000,0,0]],"nps":2,"schedule":"40","material":"A106-B"},
        {"op":"add_run","path":[[10000,0,0],[25000,0,0]]},
        {"op":"add_run","path":[[10000,0,0],[10000,20000,0]]}]})"), result));
    REQUIRE(result["ok"] == true);
    ojson doc = result["piping"];
    for (const auto& ft : doc["fittings"])
        if (ft["type"] == "tee") tee_id = ft["id"];
    REQUIRE(tee_id.is_string());
    run_a = doc["runs"][1]["id"].get<std::string>();
    run_b = doc["runs"][2]["id"].get<std::string>();
    const ojson valve = ojson::parse(R"({"k_model":{"kind":"constant","K":5.0}})");
    REQUIRE(call("piping_edit", {{"piping", doc}, {"ops", ojson::array({
        {{"op","add_fitting"},{"type","custom"},{"at",{24000,0,0}},{"run_id",run_a},{"params",valve}},
        {{"op","add_fitting"},{"type","custom"},{"at",{10000,19000,0}},{"run_id",run_b},{"params",valve}},
        {{"op","update_fitting"},{"id",tee_id},{"params",{
            {"k_model_run",{{"kind","constant"},{"K",0.38}}},
            {"k_model_branch",{{"kind","constant"},{"K",1.14}}}}}}})}}, result));
    REQUIRE(result["ok"] == true);
    return result["piping"];
}

TEST_CASE("tee: constant per-path K on run and branch matches K*rho*v^2/2 in solve and report") {
    ojson tee_id; std::string run_a, run_b;
    const ojson doc = f2_network(tee_id, run_a, run_b);
    REQUIRE(validate_piping(doc).empty());
    ojson tee;
    for (const auto& ft : doc["fittings"]) if (ft["id"] == tee_id) tee = ft;
    REQUIRE(tee["params"]["run_ids"][1] == run_a);   // straight-through leg
    REQUIRE(tee["params"]["branch_run"] == run_b);
    const double rho = 998.2, mu = 0.001002;
    const ojson boundary = ojson::parse(R"([{"at":[0,0,0],"pressure":250},
        {"at":[25000,0,0],"demand":6},{"at":[10000,20000,0],"demand":6}])");
    const ojson fluid = {{"rho", rho}, {"mu", mu}};
    ojson solved;
    REQUIRE(call("piping_solve", {{"doc", doc}, {"boundary", boundary}, {"fluid", fluid}}, solved));
    REQUIRE(solved["ok"] == true);
    REQUIRE(solved["converged"] == true);
    std::map<std::string, ojson> links;
    for (const auto& lk : solved["links"]) links[lk["id"].get<std::string>()] = lk;
    CHECK(links[doc["runs"][0]["id"].get<std::string>()]["k_total"].get<double>() == doctest::Approx(0.0));
    CHECK(links[run_a]["k_total"].get<double>() == doctest::Approx(5.38));
    CHECK(links[run_b]["k_total"].get<double>() == doctest::Approx(6.14));

    const double d = links[run_a]["id_mm"].get<double>() / 1000.0;  // NPS 2 sch 40 bore
    CHECK(d == doctest::Approx(0.05248));
    const double v = 6.0 / 3600.0 / (M_PI * d * d / 4.0);
    const double q = 0.5 * rho * v * v;
    ojson report;
    REQUIRE(call("piping_report", {{"doc", doc}, {"result", solved}, {"fluid", fluid}}, report));
    REQUIRE(report["ok"] == true);
    std::map<std::string, std::map<std::string, ojson>> rows;  // run -> fitting -> row
    for (const auto& seg : report["segments"])
        for (const auto& b : seg["breakdown"])
            if (b.contains("fitting") && b["fitting"].is_string())
                rows[seg["id"].get<std::string>()][b["fitting"].get<std::string>()] = b;
    const std::string tid = tee_id.get<std::string>();
    REQUIRE(rows[run_a].count(tid));
    REQUIRE(rows[run_b].count(tid));
    CHECK(rows[run_a][tid]["method"] == "k_model_run(constant)");
    CHECK(rows[run_b][tid]["method"] == "k_model_branch(constant)");
    CHECK(rows[run_a][tid]["dp_kpa"].get<double>() * 1000 == doctest::Approx(0.38 * q).epsilon(1e-3));
    CHECK(rows[run_b][tid]["dp_kpa"].get<double>() * 1000 == doctest::Approx(1.14 * q).epsilon(1e-3));
    for (const auto& run : {run_a, run_b}) {
        int valves = 0;
        for (const auto& [fid, row] : rows[run])
            if (fid != tid) {
                ++valves;
                CHECK(row["dp_kpa"].get<double>() * 1000 == doctest::Approx(5.0 * q).epsilon(1e-3));
            }
        CHECK(valves == 1);
    }
    // run_loss_1d (pipe_flow callback) shares the evaluator, unrounded.
    const double flow = 6.0 / 3600.0;
    const auto a = run_loss_1d(nlohmann::json(doc), run_a, rho, mu, flow, 0.046);
    const auto b = run_loss_1d(nlohmann::json(doc), run_b, rho, mu, flow, 0.046);
    REQUIRE(a["ok"] == true);
    REQUIRE(b["ok"] == true);
    CHECK(a["fittings_pa"].get<double>() == doctest::Approx(5.38 * q).epsilon(1e-3));
    CHECK(b["fittings_pa"].get<double>() == doctest::Approx(6.14 * q).epsilon(1e-3));
}

TEST_CASE("fitting_k: tee previews both paths; path selects; non-tee path refuses") {
    ojson result;
    const ojson tee = ojson::parse(R"({"type":"tee","nps":2,"params":{
        "k_model_run":{"kind":"constant","K":0.38},"k_model_branch":{"kind":"constant","K":1.14}}})");
    REQUIRE(call("piping_fitting_k", {{"fitting", tee}}, result));
    REQUIRE(result["ok"] == true);
    CHECK(result["paths"]["run"]["K"].get<double>() == doctest::Approx(0.38));
    CHECK(result["paths"]["branch"]["K"].get<double>() == doctest::Approx(1.14));
    CHECK(result["paths"]["branch"]["method"] == "k_model_branch(constant)");
    REQUIRE(call("piping_fitting_k", {{"fitting", tee}, {"path", "branch"}}, result));
    CHECK(result["K"].get<double>() == doctest::Approx(1.14));
    // k_model alone covers both paths; a path model wins over it on its own path.
    ojson both = ojson::parse(R"({"type":"tee","nps":2,"params":{
        "k_model":{"kind":"constant","K":0.9},"k_model_branch":{"kind":"constant","K":1.5}}})");
    REQUIRE(call("piping_fitting_k", {{"fitting", both}}, result));
    CHECK(result["paths"]["run"]["K"].get<double>() == doctest::Approx(0.9));
    CHECK(result["paths"]["branch"]["K"].get<double>() == doctest::Approx(1.5));
    // Without overrides a forced path uses the Crane 20/60 fT correlation.
    REQUIRE(call("piping_fitting_k", ojson::parse(R"({"fitting":{"type":"tee","nps":2},"path":"run"})"), result));
    CHECK(std::abs(result["K"].get<double>() / (20 * result["fT"].get<double>()) - 1) < 1e-9);
    REQUIRE(call("piping_fitting_k", ojson::parse(R"({"fitting":{"type":"valve","nps":2,
        "params":{"valve_type":"gate"}},"path":"run"})"), result));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "invalid_params");
    REQUIRE(call("piping_fitting_k", ojson::parse(R"({"fitting":{"type":"tee","nps":2,
        "params":{"k_model_run":{"kind":"constant","K":-1}}}})"), result));
    CHECK(result["reason"] == "invalid_k_model");
}

TEST_CASE("edit: unknown fitting params refuse by name and list accepted keys") {
    ojson tee_id; std::string run_a, run_b;
    const ojson doc = f2_network(tee_id, run_a, run_b);
    ojson result;
    REQUIRE(call("piping_edit", {{"piping", doc}, {"ops", ojson::array({
        {{"op","update_fitting"},{"id",tee_id},{"params",{{"K_run",0.38},{"K_branch",1.14}}}}})}}, result));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "unknown_param");
    CHECK(result["op_index"] == 0);
    const auto hint = result["hint"].get<std::string>();
    CHECK(hint.find("'K_run'") != std::string::npos);     // every unknown key, by name
    CHECK(hint.find("'K_branch'") != std::string::npos);
    CHECK(hint.find("k_model_run") != std::string::npos);
    CHECK(hint.find("k_model_branch") != std::string::npos);
    CHECK(!result.contains("piping"));
    // add_fitting is equally strict; per-path keys belong to tees only.
    for (const auto& params : ojson::parse(R"([{"valve_type":"ball","Kv":3},
                                               {"k_model_run":{"kind":"constant","K":1}}])")) {
        REQUIRE(call("piping_edit", {{"piping", single_pipe_doc()}, {"ops", ojson::array({
            {{"op","add_fitting"},{"type","valve"},{"at",{5000,0,0}},{"params",params}}})}}, result));
        CHECK(result["ok"] == false);
        CHECK(result["reason"] == "unknown_param");
        CHECK(result["hint"].get<std::string>().find("valve_type") != std::string::npos);
    }
    // Deleting any key (null) and re-sending stored values unchanged stay accepted,
    // so whole-params GUI round trips keep working.
    ojson tee;
    for (const auto& ft : doc["fittings"]) if (ft["id"] == tee_id) tee = ft;
    ojson params = tee["params"];
    params["k_model_branch"] = nullptr;
    params["legacy_junk"] = nullptr;
    REQUIRE(call("piping_edit", {{"piping", doc}, {"ops", ojson::array({
        {{"op","update_fitting"},{"id",tee_id},{"params",params}}})}}, result));
    REQUIRE(result["ok"] == true);
    for (const auto& ft : result["piping"]["fittings"])
        if (ft["id"] == tee_id) {
            CHECK(!ft["params"].contains("k_model_branch"));
            CHECK(ft["params"]["k_model_run"]["K"].get<double>() == doctest::Approx(0.38));
        }
    REQUIRE(call("piping_edit", {{"piping", doc}, {"ops", ojson::array({
        {{"op","update_fitting"},{"id",tee_id},{"params",{{"k_model_run",{{"kind","constant"},{"K",-1}}}}}}})}},
        result));
    CHECK(result["reason"] == "invalid_k_model");
}
