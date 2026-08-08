// core/tests/test_spice_netlist.cpp — SPICE deck parser + schematic->circuit mapper
// + Switch/MOSFET parity assertions against the Python oracle numbers.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>
#include <limits>
#include <stdexcept>

#include "sim/netlist.h"
#include "sim/spice.h"

using chudware::sim::netlist_to_spice_circuit;
using chudware::sim::parse_netlist;
using chudware::sim::spice_simulate;
using nlohmann::json;

static double pct_err(double got, double exp) {
    if (exp == 0.0) return std::abs(got);
    return std::abs(got - exp) / std::abs(exp) * 100.0;
}

TEST_CASE("parse_netlist: R + V DC deck") {
    const std::string deck =
        "* divider\n"
        "R1 in out 1k\n"
        "R2 out 0 2k\n"
        "V1 in 0 DC 3.3\n";
    json c = parse_netlist(deck);
    REQUIRE(c.contains("devices"));
    REQUIRE(c["devices"].is_array());
    CHECK(c["devices"].size() == 3);
    CHECK(c.contains("nodes"));
    CHECK(c["nodes"].is_array());
    bool saw_v = false;
    for (const json& d : c["devices"]) {
        if (d.value("kind", std::string()) == "V") {
            saw_v = true;
            CHECK(d["nodes"].size() == 2);
        }
    }
    CHECK(saw_v);
}

TEST_CASE("netlist_to_spice_circuit: Device:R libid symbol fallback") {
    json netlist = {
        {"components",
         json::array({json{{"ref", "R1"},
                           {"libid", "Device:R"},
                           {"value", "10k"},
                           {"pins", json{{"1", "N1"}, {"2", "GND"}}}}})}};
    json out = netlist_to_spice_circuit(netlist, json::object());
    REQUIRE(out.contains("circuit"));
    const json& c = out["circuit"];
    REQUIRE(c.contains("devices"));
    REQUIRE(c["devices"].is_array());
    CHECK(c["devices"].size() == 1);
    const json& dev = c["devices"][0];
    CHECK(dev.value("kind", std::string()) == "R");
    CHECK(dev.value("name", std::string()) == "R1");
    CHECK(dev["nodes"][0].get<std::string>() == "N1");
    CHECK(dev["nodes"][1].get<std::string>() == "0");  // GND -> "0"
    CHECK(dev["value"].get<double>() == doctest::Approx(10000.0));
    REQUIRE(out.contains("warnings"));
    CHECK(out["warnings"].empty());
}

TEST_CASE("parse_netlist: X OPAMP subckt expands with Dclamp model") {
    const std::string deck = "XU1 inp inn outp OPAMP\n";
    json c = parse_netlist(deck);
    REQUIRE(c.contains("devices"));
    CHECK(c["devices"].size() >= 1);
    REQUIRE(c.contains("models"));
    CHECK(c["models"].contains("Dclamp"));
}

TEST_CASE("netlist_to_spice_circuit: abstract OPAMP uses its placed unit terminals") {
    json netlist = {
        {"components",
         json::array({json{{"ref", "U1"},
                           {"libid", "Amplifier_Operational:OPAMP_dual"},
                           {"value", "OPAMP"},
                           {"pins", json{{"1", "INV"}, {"2", "IN"}, {"3", "OUT"}}}}})}};
    json out = netlist_to_spice_circuit(netlist, json::object());
    REQUIRE(out["warnings"].empty());
    REQUIRE_FALSE(out["circuit"]["devices"].empty());
    bool saw_out = false;
    for (const json& device : out["circuit"]["devices"])
        for (const json& node : device["nodes"])
            saw_out = saw_out || node == "OUT";
    CHECK(saw_out);
}

TEST_CASE("netlist_to_spice_circuit: unknown libid warns and skips") {
    json netlist = {{"components",
                     json::array({json{{"ref", "U1"},
                                       {"libid", "Custom:UnknownChip"},
                                       {"pins", json{{"1", "A"}}}}})}};
    json out = netlist_to_spice_circuit(netlist, json::object());
    CHECK(out["circuit"]["devices"].empty());
    REQUIRE(out["warnings"].is_array());
    CHECK(out["warnings"].size() == 1);
    CHECK(out["warnings"][0].get<std::string>().find("no sim model") != std::string::npos);
}

// ---------------------------------------------------------------------------
// Switch S parity vs Python oracle (engine.sim.test_circuit::test_switch_ron_roff)
// ---------------------------------------------------------------------------
TEST_CASE("spice_simulate: Switch S on/off resistance within 1%") {
    const double Vsrc = 1.0;
    const double Rseries = 1000.0;
    const double ron = 10.0;
    const double roff = 1.0e6;
    const double vt = 0.5;
    const double vh = 0.1;

    auto run = [&](double vctrl) -> double {
        json circuit = {
            {"nodes", json::array({"0", "OUT", "CTRL", "SRC"})},
            {"devices", json::array({
                json{{"kind", "V"}, {"name", "VS"}, {"nodes", json::array({"SRC", "0"})},
                     {"value", Vsrc}, {"params", json::object()}},
                json{{"kind", "R"}, {"name", "RS"}, {"nodes", json::array({"SRC", "OUT"})},
                     {"value", Rseries}, {"params", json::object()}},
                json{{"kind", "V"}, {"name", "VC"}, {"nodes", json::array({"CTRL", "0"})},
                     {"value", vctrl}, {"params", json::object()}},
                json{{"kind", "S"}, {"name", "S1"},
                     {"nodes", json::array({"OUT", "0", "CTRL", "0"})},
                     {"value", nullptr},
                     {"params", json{{"vt", vt}, {"vh", vh}, {"ron", ron}, {"roff", roff}}}},
            })},
            {"models", json::object()},
            {"params", json{{"TEMP", 27}}},
        };
        json study = {{"analyses", json::array({json{{"type", "op"}}})},
                      {"probes", json::array({"V(OUT)"})}};
        json res = spice_simulate(circuit, study);
        REQUIRE(res["convergence"]["op"].get<bool>());
        return res["op"]["V(OUT)"].get<double>();
    };

    double v_on = run(vt + vh + 0.5);
    double r_on = Rseries * v_on / std::max(Vsrc - v_on, 1e-30);
    CHECK(pct_err(r_on, ron) < 1.0);

    double v_off = run(vt - vh - 0.5);
    double r_off = Rseries * v_off / std::max(Vsrc - v_off, 1e-30);
    CHECK(pct_err(r_off, roff) < 1.0);

    // Python oracle (engine.sim.test_circuit) measures exactly ron=10 / roff=1e6.
    CHECK(pct_err(r_on, 10.0) < 1.0);
    CHECK(pct_err(r_off, 1.0e6) < 1.0);
}

// ---------------------------------------------------------------------------
// MOSFET AC Cgs pole parity vs Python oracle (fc = 1/(2 pi R Cgs) within 5%)
// ---------------------------------------------------------------------------
TEST_CASE("spice_simulate: MOSFET AC Cgs pole within 5%") {
    const double Rdrive = 1000.0;
    const double Cgs = 1e-9;
    const double fc_exp = 1.0 / (2.0 * 3.14159265358979323846 * Rdrive * Cgs);
    const double VDD = 5.0;
    const double VGS_bias = 2.0;

    json circuit = {
        {"nodes", json::array({"0", "VDD", "G", "D", "GIN"})},
        {"devices", json::array({
            json{{"kind", "V"}, {"name", "VDD"}, {"nodes", json::array({"VDD", "0"})},
                 {"value", VDD}, {"params", json::object()}},
            json{{"kind", "V"}, {"name", "VG"}, {"nodes", json::array({"GIN", "0"})},
                 {"value", VGS_bias},
                 {"params", json{{"ac", json{{"mag", 1.0}, {"phase", 0.0}}}}}},
            json{{"kind", "R"}, {"name", "RG"}, {"nodes", json::array({"GIN", "G"})},
                 {"value", Rdrive}, {"params", json::object()}},
            json{{"kind", "R"}, {"name", "RD"}, {"nodes", json::array({"VDD", "D"})},
                 {"value", 2000.0}, {"params", json::object()}},
            json{{"kind", "M"}, {"name", "M1"}, {"nodes", json::array({"D", "G", "0"})},
                 {"value", nullptr}, {"model", "NM"}, {"params", json::object()}},
        })},
        {"models", json{{"NM", json{{"type", "nmos"}, {"VTO", 1.0}, {"KP", 2e-4},
                                    {"W", 10.0}, {"L", 1.0}, {"LAMBDA", 0.0},
                                    {"CGS", Cgs}, {"CGD", 0.0}, {"CDS", 0.0}}}}},
        {"params", json{{"TEMP", 27}}},
    };
    json study = {
        {"analyses", json::array({
            json{{"type", "op"}},
            json{{"type", "ac"}, {"sweep", "dec"}, {"points", 20},
                 {"fstart", fc_exp / 100.0}, {"fstop", fc_exp * 100.0}},
        })},
        {"probes", json::array({"V(G)"})},
    };
    json res = spice_simulate(circuit, study);
    REQUIRE(res["convergence"]["op"].get<bool>());
    REQUIRE(res["convergence"]["ac"].get<bool>());

    const json& freqs = res["ac"]["x_f"];
    const json& mag = res["ac"]["mag"]["V(G)"];
    REQUIRE(freqs.is_array());
    REQUIRE(mag.is_array());
    REQUIRE(freqs.size() == mag.size());
    REQUIRE(freqs.size() > 2);

    double low = mag[0].get<double>();
    REQUIRE(low > 0.5);
    double target = low / std::sqrt(2.0);
    int idx = -1;
    for (size_t i = 0; i < mag.size(); ++i) {
        if (mag[i].get<double>() <= target) { idx = static_cast<int>(i); break; }
    }
    REQUIRE(idx >= 0);
    double fc_meas;
    if (idx == 0) {
        fc_meas = freqs[0].get<double>();
    } else {
        double m0 = mag[idx - 1].get<double>();
        double m1 = mag[idx].get<double>();
        double f0 = freqs[idx - 1].get<double>();
        double f1 = freqs[idx].get<double>();
        if (m0 == m1) {
            fc_meas = f1;
        } else {
            double t = (target - m0) / (m1 - m0);
            fc_meas = std::exp(std::log(f0) + t * (std::log(f1) - std::log(f0)));
        }
    }
    // Python oracle measures ~159170 Hz (0.01% off analytic 159155).
    CHECK(pct_err(fc_meas, fc_exp) < 5.0);
    CHECK(pct_err(fc_meas, 159170.0) < 1.0);  // parity vs Python within 1%
}

TEST_CASE("parse_netlist: controlled-source gain reaches the MNA stamp") {
    const json circuit = parse_netlist(
        "V1 in 0 DC 1\n"
        "E1 out 0 in 0 2\n"
        "R1 out 0 1k\n");
    REQUIRE(circuit["devices"].size() == 3);
    REQUIRE(circuit["devices"][1]["params"]["gain"].get<double>() ==
            doctest::Approx(2.0));

    const json study = {
        {"analyses", json::array({json{{"type", "op"}}})},
        {"probes", json::array({"V(out)"})},
    };
    const json result = spice_simulate(circuit, study);
    REQUIRE(result["convergence"]["op"].get<bool>());
    CHECK(result["op"]["V(out)"].get<double>() == doctest::Approx(2.0).epsilon(1e-10));
    CHECK_THROWS_AS(parse_netlist("Rbad in 0 not-a-number\n"),
                    std::invalid_argument);
    CHECK_THROWS_AS(parse_netlist("Rnegative in 0 -1\n"),
                    std::invalid_argument);
    CHECK_THROWS_AS(parse_netlist("Vbad in 0 PWL(0 0 1)\n"),
                    std::invalid_argument);
    const json mixed_source = parse_netlist("Vmix in 0 AC 2 DC 1\n");
    REQUIRE(mixed_source["devices"].size() == 1);
    CHECK(mixed_source["devices"][0]["value"].get<double>() == doctest::Approx(1.0));
    CHECK(mixed_source["devices"][0]["params"]["ac"]["mag"].get<double>() ==
          doctest::Approx(2.0));
}

TEST_CASE("spice_simulate: singular MNA and non-finite setup fail honestly") {
    const json singular = {
        {"nodes", json::array({"0", "N"})},
        {"devices", json::array({
            json{{"kind", "V"}, {"name", "V1"}, {"nodes", json::array({"N", "0"})},
                 {"value", 1.0}, {"params", json::object()}},
            json{{"kind", "V"}, {"name", "V2"}, {"nodes", json::array({"N", "0"})},
                 {"value", 2.0}, {"params", json::object()}},
        })},
        {"models", json::object()},
        {"params", json::object()},
    };
    const json study = {
        {"analyses", json::array({json{{"type", "op"}}})},
        {"probes", json::array({"V(N)"})},
        {"options", json{{"gmin", 0.0}}},
    };
    const json result = spice_simulate(singular, study);
    CHECK_FALSE(result["convergence"]["op"].get<bool>());
    CHECK(result["op"]["V(N)"].is_null());

    json nonfinite = singular;
    nonfinite["devices"][0]["value"] = std::numeric_limits<double>::quiet_NaN();
    const json invalid = spice_simulate(nonfinite, study);
    CHECK_FALSE(invalid["convergence"]["setup"].get<bool>());
    CHECK(invalid.value("cache_reason", std::string()) == "invalid_input");
}

TEST_CASE("spice_simulate: setup requires excitation and valid Monte-Carlo seed") {
    json circuit = {
        {"nodes", json::array({"0", "OUT"})},
        {"devices", json::array({
            json{{"kind", "V"}, {"name", "V1"}, {"nodes", json::array({"OUT", "0"})},
                 {"value", 0.0}, {"params", json::object()}},
            json{{"kind", "R"}, {"name", "R1"}, {"nodes", json::array({"OUT", "0"})},
                 {"value", 1000.0}, {"params", json::object()}},
        })},
        {"models", json::object()},
        {"params", json::object()},
    };
    const json op = {{"analyses", json::array({json{{"type", "op"}}})},
                     {"probes", json::array({"V(OUT)"})}};
    const json zero = spice_simulate(circuit, op);
    CHECK_FALSE(zero["convergence"]["setup"].get<bool>());

    circuit["devices"][0]["value"] = 1.0;
    json uppercase_op = op;
    uppercase_op["analyses"][0]["type"] = "OP";
    const json uppercase_result = spice_simulate(circuit, uppercase_op);
    CHECK(uppercase_result["convergence"]["op"].get<bool>());
    const json mc = {
        {"analyses", json::array({
            json{{"type", "mc"}, {"runs", 2}, {"seed", -1}, {"probe", "V(OUT)"}},
        })},
        {"probes", json::array({"V(OUT)"})},
    };
    const json bad_seed = spice_simulate(circuit, mc);
    CHECK_FALSE(bad_seed["convergence"]["setup"].get<bool>());

    json fractional_seed = mc;
    fractional_seed["analyses"][0]["seed"] = 1.5;
    const json bad_fraction = spice_simulate(circuit, fractional_seed);
    CHECK_FALSE(bad_fraction["convergence"]["setup"].get<bool>());

    json partial_mc = mc;
    partial_mc["analyses"][0]["seed"] = 12345;
    partial_mc["analyses"][0]["runs"] = 4;

    json unsupported_bulk = circuit;
    unsupported_bulk["devices"].push_back(
        json{{"kind", "M"}, {"name", "M1"},
             {"nodes", json::array({"OUT", "OUT", "0", "BULK"})},
             {"value", nullptr}, {"params", json::object()}});
    const json bulk_result = spice_simulate(unsupported_bulk, op);
    CHECK_FALSE(bulk_result["convergence"]["setup"].get<bool>());
    partial_mc["analyses"][0]["tol"] = json{{"R", 2.0}};
    const json partial = spice_simulate(circuit, partial_mc);
    CHECK_FALSE(partial["convergence"]["mc"].get<bool>());
    CHECK(partial["mc"]["requested_runs"].get<int>() == 4);
    CHECK(partial["mc"]["attempted_runs"].get<int>() == 4);
    CHECK(partial["mc"]["runs"].get<int>() == 2);
    CHECK(partial["mc"]["failed_runs"].get<int>() == 2);
    CHECK(partial["mc"]["values"].size() == 2);
}

TEST_CASE("spice_simulate: diode contributes its AC small-signal Jacobian") {
    const json circuit = {
        {"nodes", json::array({"0", "IN", "OUT"})},
        {"devices", json::array({
            json{{"kind", "V"}, {"name", "V1"}, {"nodes", json::array({"IN", "0"})},
                 {"value", 0.7},
                 {"params", json{{"ac", json{{"mag", 1.0}, {"phase", 0.0}}}}}},
            json{{"kind", "R"}, {"name", "R1"}, {"nodes", json::array({"IN", "OUT"})},
                 {"value", 1000.0}, {"params", json::object()}},
            json{{"kind", "D"}, {"name", "D1"}, {"nodes", json::array({"OUT", "0"})},
                 {"value", nullptr}, {"params", json::object()}},
        })},
        {"models", json::object()},
        {"params", json::object()},
    };
    const json study = {
        {"analyses", json::array({
            json{{"type", "op"}},
            json{{"type", "ac"}, {"sweep", "lin"}, {"points", 2},
                 {"fstart", 1.0}, {"fstop", 10.0}},
        })},
        {"probes", json::array({"V(OUT)"})},
    };
    const json result = spice_simulate(circuit, study);
    REQUIRE(result["convergence"]["op"].get<bool>());
    REQUIRE(result["convergence"]["ac"].get<bool>());
    REQUIRE(result["ac"]["mag"]["V(OUT)"].size() == 2);
    CHECK(result["ac"]["mag"]["V(OUT)"][0].get<double>() < 1.0);
}

TEST_CASE("netlist_to_spice_circuit: transistor pin order and unresolved pins are safe") {
    const json netlist = {
        {"components", json::array({
            json{{"ref", "Q1"}, {"libid", "Device:Q_NPN"}, {"value", ""},
                 {"pins", json{{"B", "NB"}, {"C", "NC"}, {"E", "NE"}}}},
            json{{"ref", "M1"}, {"libid", "Device:NMOS"}, {"value", ""},
                 {"pins", json{{"G", "NG"}, {"D", "ND"}, {"S", "NS"}}}},
            json{{"ref", "Rbad"}, {"libid", "Device:R"}, {"value", "1k"},
                 {"pins", json{{"1", "N1"}}}},
        })},
    };
    const json mapped = netlist_to_spice_circuit(netlist, json::object());
    REQUIRE(mapped["circuit"]["devices"].size() == 2);
    CHECK(mapped["circuit"]["devices"][0]["nodes"] == json::array({"NC", "NB", "NE"}));
    CHECK(mapped["circuit"]["devices"][1]["nodes"] == json::array({"ND", "NG", "NS"}));
    REQUIRE_FALSE(mapped["warnings"].empty());
    CHECK(mapped["warnings"][0].get<std::string>().find("component skipped") !=
          std::string::npos);
}

TEST_CASE("spice_simulate: non-converged transient cannot feed Monte Carlo or Fourier") {
    const json circuit = {
        {"nodes", json::array({"0", "IN", "OUT"})},
        {"devices", json::array({
            json{{"kind", "V"}, {"name", "V1"}, {"nodes", json::array({"IN", "0"})},
                 {"value", 0.0},
                 {"params", json{{"waveform", json{{"type", "PULSE"}, {"v1", 0.0},
                                                    {"v2", 5.0}, {"td", 0.0},
                                                    {"tr", 1e-12}, {"tf", 1e-12},
                                                    {"pw", 1e-3}, {"per", 2e-3}}}}}},
            json{{"kind", "R"}, {"name", "R1"}, {"nodes", json::array({"IN", "OUT"})},
                 {"value", 1.0}, {"params", json::object()}},
            json{{"kind", "D"}, {"name", "D1"}, {"nodes", json::array({"OUT", "0"})},
                 {"value", nullptr}, {"params", json::object()}},
        })},
        {"models", json::object()},
        {"params", json::object()},
    };
    const json tran = {
        {"type", "tran"}, {"tstep", 1e-6}, {"tstop", 2e-6},
    };
    const json study = {
        {"analyses", json::array({tran})},
        {"probes", json::array({"V(OUT)"})},
        {"options", json{{"maxiter", 1}}},
    };
    const json result = spice_simulate(circuit, study);
    CHECK_FALSE(result["convergence"]["tran"].get<bool>());
    REQUIRE(result["tran"]["x_t"].size() == 1);
    REQUIRE(result["tran"]["traces"]["V(OUT)"].size() == 1);
    CHECK(result["tran"]["traces"]["V(OUT)"][0].is_number());

    const json mc_study = {
        {"analyses", json::array({
            json{{"type", "mc"}, {"runs", 3}, {"seed", 7}, {"tol", 0.0},
                 {"probe", "V(OUT)"}, {"analysis", tran}},
        })},
        {"probes", json::array({"V(OUT)"})},
        {"options", json{{"maxiter", 1}}},
    };
    const json mc = spice_simulate(circuit, mc_study);
    CHECK_FALSE(mc["convergence"]["mc"].get<bool>());
    CHECK(mc["mc"]["requested_runs"].get<int>() == 3);
    CHECK(mc["mc"]["attempted_runs"].get<int>() == 3);
    CHECK(mc["mc"]["runs"].get<int>() == 0);
    CHECK(mc["mc"]["failed_runs"].get<int>() == 3);
    CHECK(mc["mc"]["values"].empty());
    bool saw_mc_inner_failure = false;
    for (const json& entry : mc["log"])
        if (entry.is_string() &&
            entry.get<std::string>().find("tran: non-convergence") != std::string::npos)
            saw_mc_inner_failure = true;
    CHECK(saw_mc_inner_failure);

    json delayed = circuit;
    delayed["devices"][0]["params"]["waveform"]["td"] = 10e-6;
    const json four_study = {
        {"analyses", json::array({
            json{{"type", "tran"}, {"tstep", 1e-6}, {"tstop", 20e-6}},
            json{{"type", "four"}, {"f0", 1e6}, {"nharm", 3}, {"probe", "V(OUT)"}},
        })},
        {"probes", json::array({"V(OUT)"})},
        {"options", json{{"maxiter", 1}}},
    };
    const json partial_four = spice_simulate(delayed, four_study);
    CHECK_FALSE(partial_four["convergence"]["tran"].get<bool>());
    REQUIRE(partial_four["tran"]["x_t"].size() >= 4);
    CHECK_FALSE(partial_four["convergence"]["four"].get<bool>());
    CHECK_FALSE(partial_four.contains("four"));
    bool saw_four_prerequisite = false;
    for (const json& entry : partial_four["log"])
        if (entry.is_string() &&
            entry.get<std::string>().find("partial trace retained") != std::string::npos)
            saw_four_prerequisite = true;
    CHECK(saw_four_prerequisite);

    const json recursive_four_study = {
        {"analyses", json::array({
            json{{"type", "four"}, {"tstep", 1e-6}, {"tstop", 20e-6},
                 {"f0", 1e6}, {"nharm", 3}, {"probe", "V(OUT)"}},
        })},
        {"probes", json::array({"V(OUT)"})},
        {"options", json{{"maxiter", 1}}},
    };
    const json recursive_four = spice_simulate(delayed, recursive_four_study);
    CHECK_FALSE(recursive_four["convergence"]["tran"].get<bool>());
    REQUIRE(recursive_four["tran"]["x_t"].size() >= 4);
    CHECK_FALSE(recursive_four["convergence"]["four"].get<bool>());
    CHECK_FALSE(recursive_four.contains("four"));
    bool saw_recursive_prerequisite = false;
    for (const json& entry : recursive_four["log"])
        if (entry.is_string() &&
            entry.get<std::string>().find("partial trace retained") != std::string::npos)
            saw_recursive_prerequisite = true;
    CHECK(saw_recursive_prerequisite);
}

TEST_CASE("spice_simulate: transient tstart controls the published trace window") {
    const json circuit = parse_netlist(
        "V1 out 0 PULSE(0 1 0 1n 1n 1m 2m)\n"
        "R1 out 0 1k\n");
    const json study = {
        {"analyses", json::array({
            json{{"type", "tran"}, {"tstep", 0.5e-6}, {"tstop", 2e-6},
                 {"tstart", 1e-6}},
        })},
        {"probes", json::array({"V(out)"})},
    };
    const json result = spice_simulate(circuit, study);
    REQUIRE(result["convergence"]["tran"].get<bool>());
    REQUIRE_FALSE(result["tran"]["x_t"].empty());
    CHECK(result["tran"]["x_t"][0].get<double>() ==
          doctest::Approx(1e-6).epsilon(1e-12));
    CHECK(result["tran"]["traces"]["V(out)"].size() ==
          result["tran"]["x_t"].size());
}
