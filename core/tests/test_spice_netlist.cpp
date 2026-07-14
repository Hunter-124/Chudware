// core/tests/test_spice_netlist.cpp — SPICE deck parser + schematic->circuit mapper
// + Switch/MOSFET parity assertions against the Python oracle numbers.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>

#include "sim/netlist.h"
#include "sim/spice.h"

using aicad::sim::netlist_to_spice_circuit;
using aicad::sim::parse_netlist;
using aicad::sim::spice_simulate;
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
