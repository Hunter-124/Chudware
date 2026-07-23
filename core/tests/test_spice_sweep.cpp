// core/tests/test_spice_sweep.cpp — parallel SPICE parameter sweep.
//
// (a) R-divider sweep [1k,2k,5k,10k] matches serial single-run results (exact)
// (b) Parallel uses >1 worker when governor allots them; not slower than serial
//     for a big sweep (both wall-times printed)
// (c) Result order matches input order
// (d) 1-point sweep works
//
// Deterministic fixed inputs. Wall-times are printed (no expected-time asserts
// beyond "parallel not slower than serial" with a small slack).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "sim/sim_cache.h"
#include "sim/spice.h"
#include "sim/spice_sweep.h"
#include "util/resource_governor.h"
#include "util/thread_pool.h"

using nlohmann::json;
using chudware::sim::spice_simulate;
using chudware::sim::spice_sweep;
using chudware::sim::spice_sweep_serial;
using chudware::sim::SpiceSweepStats;

namespace {

// Voltage divider: V=3.3 across R1 (top, swept) and R2=2k to ground; probe V(OUT).
json divider_circuit(double r1 = 1000.0, double r2 = 2000.0, double v = 3.3) {
    return json{
        {"nodes", json::array({"0", "in", "OUT"})},
        {"devices",
         json::array({
             json{{"kind", "V"},
                  {"name", "V1"},
                  {"nodes", json::array({"in", "0"})},
                  {"value", v},
                  {"params", json::object()}},
             json{{"kind", "R"},
                  {"name", "R1"},
                  {"nodes", json::array({"in", "OUT"})},
                  {"value", r1},
                  {"params", json::object()}},
             json{{"kind", "R"},
                  {"name", "R2"},
                  {"nodes", json::array({"OUT", "0"})},
                  {"value", r2},
                  {"params", json::object()}},
         })},
        {"models", json::object()},
        {"params", json{{"TEMP", 27}}},
    };
}

json op_study() {
    return json{{"analyses", json::array({json{{"type", "op"}}})},
                {"probes", json::array({"V(OUT)"})}};
}

// Compare physical OP outputs. Cache metadata may differ between paths.
bool op_equal(const json& a, const json& b, double eps = 1e-12) {
    if (!a.contains("op") || !b.contains("op")) return false;
    if (!a["op"].contains("V(OUT)") || !b["op"].contains("V(OUT)")) return false;
    const double va = a["op"]["V(OUT)"].get<double>();
    const double vb = b["op"]["V(OUT)"].get<double>();
    if (!std::isfinite(va) || !std::isfinite(vb)) return false;
    if (std::abs(va - vb) > eps) return false;
    if (a.contains("convergence") && b.contains("convergence")) {
        if (a["convergence"].value("op", false) != b["convergence"].value("op", false))
            return false;
    }
    return true;
}

void put_env(const char* k, const char* v) {
#if defined(_WIN32)
    if (v) _putenv_s(k, v);
    else _putenv_s(k, "");
#else
    if (v) setenv(k, v, 1);
    else unsetenv(k);
#endif
}

}  // namespace

TEST_CASE("spice_sweep: 1-point sweep works") {
    const auto t0 = std::chrono::steady_clock::now();
    json circuit = divider_circuit();
    json study = op_study();
    std::vector<double> values = {1000.0};
    SpiceSweepStats st;
    auto out = spice_sweep(circuit, "R1", values, study, &st);
    REQUIRE(out.size() == 1);
    REQUIRE(out[0]["convergence"].value("op", false));
    // Vout = 3.3 * 2k/(1k+2k) = 2.2
    CHECK(out[0]["op"]["V(OUT)"].get<double>() == doctest::Approx(2.2).epsilon(1e-9));
    CHECK(out[0]["value"].get<double>() == doctest::Approx(1000.0));
    CHECK(st.n_points == 1);
    const double ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
            .count();
    std::printf("[spice_sweep] 1-point V(OUT)=%.6f wall=%.3f ms (sweep_stats=%.3f ms)\n",
                out[0]["op"]["V(OUT)"].get<double>(), ms, st.wall_ms);
}

TEST_CASE("spice_sweep: divider R sweep parity + order vs serial single-runs") {
    const auto t0 = std::chrono::steady_clock::now();
    chudware::sim::cache::clear();
    json circuit = divider_circuit();
    json study = op_study();
    const std::vector<double> values = {1000.0, 2000.0, 5000.0, 10000.0};

    // Serial single-run baseline (hand-applied param, direct spice_simulate).
    std::vector<json> serial_direct;
    serial_direct.reserve(values.size());
    for (double r : values) {
        json c = divider_circuit(r);
        json res = spice_simulate(c, study);
        res["value"] = r;
        serial_direct.push_back(std::move(res));
    }

    SpiceSweepStats serial_st, parallel_st;
    auto serial = spice_sweep_serial(circuit, "R1", values, study, &serial_st);
    auto parallel = spice_sweep(circuit, "R1", values, study, &parallel_st);

    REQUIRE(serial.size() == values.size());
    REQUIRE(parallel.size() == values.size());
    REQUIRE(serial_direct.size() == values.size());

    // (c) order matches input order
    for (std::size_t i = 0; i < values.size(); ++i) {
        CHECK(parallel[i]["value"].get<double>() == doctest::Approx(values[i]));
        CHECK(serial[i]["value"].get<double>() == doctest::Approx(values[i]));
    }

    // (a) exact parity: parallel == serial helper == direct single-runs
    for (std::size_t i = 0; i < values.size(); ++i) {
        REQUIRE(parallel[i]["convergence"].value("op", false));
        REQUIRE(serial[i]["convergence"].value("op", false));
        REQUIRE(serial_direct[i]["convergence"].value("op", false));
        CHECK(op_equal(parallel[i], serial[i]));
        CHECK(op_equal(parallel[i], serial_direct[i]));
        // Analytic: Vout = 3.3 * R2 / (R1 + R2), R2=2000
        const double expect = 3.3 * 2000.0 / (values[i] + 2000.0);
        CHECK(parallel[i]["op"]["V(OUT)"].get<double>() ==
              doctest::Approx(expect).epsilon(1e-9));
    }

    const double ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
            .count();
    std::printf(
        "[spice_sweep] parity R=[1k,2k,5k,10k] serial=%.3f ms parallel=%.3f ms "
        "workers=%d total=%.3f ms\n",
        serial_st.wall_ms, parallel_st.wall_ms, parallel_st.workers, ms);
    for (std::size_t i = 0; i < values.size(); ++i) {
        std::printf("  R1=%.0f V(OUT)=%.9f\n", values[i],
                    parallel[i]["op"]["V(OUT)"].get<double>());
    }
}

TEST_CASE("spice_sweep: big sweep parallel not slower than serial + multi-worker") {
    // Force a multi-worker allotment so (b) is testable on any host.
    put_env("CHUDWARE_GOVERNOR", nullptr);
    put_env("CHUDWARE_SIM_THREADS", "4");
    (void)chudware::pool::reconfigure_shared(0, "spice_sweep_test_prep");
    chudware::sim::cache::clear();

    json circuit = divider_circuit();
    json study = op_study();
    // Enough independent OP points that pool dispatch beats a serial loop when
    // workers > 1. 48 points keeps the test under a second even serially.
    std::vector<double> values;
    values.reserve(48);
    for (int i = 0; i < 48; ++i) values.push_back(500.0 + 250.0 * i);  // 500..12250
    SpiceSweepStats serial_st, parallel_st;
    auto serial = spice_sweep_serial(circuit, "R1", values, study, &serial_st);
    chudware::sim::cache::clear();  // cold parallel: measure real solve, not cache hits
    auto parallel = spice_sweep(circuit, "R1", values, study, &parallel_st);

    REQUIRE(serial.size() == values.size());
    REQUIRE(parallel.size() == values.size());

    // Order + parity across the full set.
    for (std::size_t i = 0; i < values.size(); ++i) {
        CHECK(parallel[i]["value"].get<double>() == doctest::Approx(values[i]));
        REQUIRE(parallel[i]["convergence"].value("op", false));
        CHECK(op_equal(parallel[i], serial[i]));
    }

    // (b) governor allots >1 worker under CHUDWARE_SIM_THREADS=4.
    CHECK(parallel_st.workers > 1);
    CHECK(chudware::pool::shared().max_workers() >= parallel_st.workers);

    // Parallel must not be slower than serial (allow 10% slack for scheduling noise).
    CHECK(parallel_st.wall_ms <= serial_st.wall_ms * 1.10 + 1.0);

    std::printf(
        "[spice_sweep] big N=%d serial=%.3f ms parallel=%.3f ms workers=%d "
        "speedup=%.2fx plan='%s'\n",
        parallel_st.n_points, serial_st.wall_ms, parallel_st.wall_ms, parallel_st.workers,
        (parallel_st.wall_ms > 0.0) ? (serial_st.wall_ms / parallel_st.wall_ms) : 0.0,
        parallel_st.plan_report.c_str());

    // Restore a modest shared pool for any later tests in-process.
    put_env("CHUDWARE_SIM_THREADS", "2");
    (void)chudware::pool::reconfigure_shared(0, "spice_sweep_test_restore");
    put_env("CHUDWARE_SIM_THREADS", nullptr);
}

TEST_CASE("spice_sweep: empty values returns empty") {
    SpiceSweepStats st;
    auto out = spice_sweep(divider_circuit(), "R1", {}, op_study(), &st);
    CHECK(out.empty());
    CHECK(st.n_points == 0);
}
