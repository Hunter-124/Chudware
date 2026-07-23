// Parallel SPICE parameter sweep — see spice_sweep.h.
#include "sim/spice_sweep.h"

#include "sim/spice.h"
#include "util/resource_governor.h"
#include "util/thread_pool.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <string>
#include <vector>

namespace chudware::sim {
namespace {

using nlohmann::json;
using clock = std::chrono::steady_clock;

std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return s;
}
std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

// Mirror spice.cpp's apply_step_param: device name match sets value; else circuit
// params (case-insensitive key, or insert as-is).
void apply_step_param(json& circ, const std::string& param, double value) {
    bool hit = false;
    if (circ.contains("devices") && circ["devices"].is_array()) {
        for (json& d : circ["devices"]) {
            if (d.value("name", std::string()) == param) {
                d["value"] = value;
                hit = true;
            }
        }
    }
    if (hit) return;
    if (!circ.contains("params") || !circ["params"].is_object()) circ["params"] = json::object();
    json& params = circ["params"];
    for (const std::string& key : {param, upper(param), lower(param)}) {
        if (params.contains(key)) {
            params[key] = value;
            return;
        }
    }
    params[param] = value;
}

// Normalize `analysis` into a full study object (analyses + probes).
json make_study(const json& analysis) {
    if (analysis.is_object() && analysis.contains("analyses") && analysis["analyses"].is_array()) {
        return analysis;
    }
    json study = json::object();
    if (analysis.is_object() && analysis.contains("type")) {
        study["analyses"] = json::array({analysis});
    } else {
        study["analyses"] = json::array({json{{"type", "op"}}});
    }
    if (analysis.is_object() && analysis.contains("probes")) {
        study["probes"] = analysis["probes"];
    } else {
        study["probes"] = json::array();
    }
    if (analysis.is_object() && analysis.contains("options")) {
        study["options"] = analysis["options"];
    }
    return study;
}

// Rough per-point working-set estimate so the governor can size workers without
// oversubscribing RAM. Small OP circuits are ~tens of KB; pad generously.
uint64_t estimate_sweep_bytes(const json& circuit, int n_points) {
    const int n_dev = (circuit.contains("devices") && circuit["devices"].is_array())
                          ? static_cast<int>(circuit["devices"].size())
                          : 4;
    const int n_nodes = (circuit.contains("nodes") && circuit["nodes"].is_array())
                            ? static_cast<int>(circuit["nodes"].size())
                            : 4;
    // Dense MNA ~ O(n^2) doubles + JSON clones + Eigen temps.
    const uint64_t per =
        static_cast<uint64_t>(std::max(n_dev, 1) + std::max(n_nodes, 1)) * 4096ull + (256ull << 10);
    return per * static_cast<uint64_t>(std::max(n_points, 1));
}

json run_one(const json& circuit, const std::string& param, double value, const json& study) {
    json c = circuit;  // deep copy
    apply_step_param(c, param, value);
    json res = spice_simulate(c, study);
    res["value"] = value;
    return res;
}

}  // namespace

std::vector<json> spice_sweep_serial(const json& circuit, const std::string& param,
                                     const std::vector<double>& values, const json& analysis,
                                     SpiceSweepStats* stats) {
    const auto t0 = clock::now();
    const json study = make_study(analysis);
    std::vector<json> out;
    out.reserve(values.size());
    for (double v : values) out.push_back(run_one(circuit, param, v, study));
    if (stats) {
        stats->workers = 1;
        stats->n_points = static_cast<int>(values.size());
        stats->wall_ms =
            std::chrono::duration<double, std::milli>(clock::now() - t0).count();
        stats->plan_report = "serial";
    }
    return out;
}

std::vector<json> spice_sweep(const json& circuit, const std::string& param,
                              const std::vector<double>& values, const json& analysis,
                              SpiceSweepStats* stats) {
    const auto t0 = clock::now();
    const int n = static_cast<int>(values.size());
    if (n <= 0) {
        if (stats) {
            stats->workers = 1;
            stats->n_points = 0;
            stats->wall_ms = 0.0;
            stats->plan_report = "empty";
        }
        return {};
    }

    // 1-point: no pool overhead — same path as serial.
    if (n == 1) {
        auto out = spice_sweep_serial(circuit, param, values, analysis, stats);
        if (stats) stats->plan_report = "single-point";
        return out;
    }

    const json study = make_study(analysis);
    const uint64_t est = estimate_sweep_bytes(circuit, n);
    const chudware::governor::Plan plan =
        chudware::governor::plan_for(est, "spice_sweep", /*want_workers=*/0);
    // Cap workers at point count (no point oversubscribing empty slots).
    const int workers = std::max(1, std::min(plan.workers, n));
    chudware::pool::shared().set_max_workers(workers);

    std::vector<json> out(static_cast<std::size_t>(n));
    chudware::pool::shared().parallel_for(n, [&](int i) {
        out[static_cast<std::size_t>(i)] =
            run_one(circuit, param, values[static_cast<std::size_t>(i)], study);
    });

    if (stats) {
        stats->workers = workers;
        stats->n_points = n;
        stats->wall_ms =
            std::chrono::duration<double, std::milli>(clock::now() - t0).count();
        stats->plan_report = plan.report;
    }
    return out;
}

}  // namespace chudware::sim
