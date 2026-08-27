// Batched SPICE parameter sweep — N independent points of the same circuit with
// one component/param varied (`.step` / Monte-Carlo style). Points run in
// parallel on the Wave-1 shared chudware::pool, sized by resource_governor::plan_for.
//
// Each point invokes the single-run spice_simulate entry (sim/spice.h). Results
// are returned in input order and are bit-identical (same JSON payload) to the
// corresponding serial single-run results.
#pragma once

#include <nlohmann/json.hpp>
#include "sim/sim_cancel.h"

#include <string>
#include <vector>

namespace chudware::sim {

// Observability for the last (or this) sweep: workers the governor allotted,
// point count, wall time, and the plan report string.
struct SpiceSweepStats {
    int workers = 1;
    int n_points = 0;
    double wall_ms = 0.0;
    std::string plan_report;
};

// Parallel sweep. `analysis` is either a full study object
//   {"analyses":[...], "probes":[...], "options":...}
// or a single analysis dict with a "type" key (wrapped into a one-entry study).
// Each returned element is the spice_simulate result for that point, plus
// "value" = the swept parameter value.
std::vector<nlohmann::json> spice_sweep(const nlohmann::json& circuit,
                                        const std::string& param,
                                        const std::vector<double>& values,
                                        const nlohmann::json& analysis,
                                        SpiceSweepStats* stats = nullptr,
                                        const SimCancelToken& cancel_token = {});

// Serial baseline with identical semantics (for parity / wall-time comparison).
std::vector<nlohmann::json> spice_sweep_serial(const nlohmann::json& circuit,
                                               const std::string& param,
                                               const std::vector<double>& values,
                                               const nlohmann::json& analysis,
                                               SpiceSweepStats* stats = nullptr,
                                               const SimCancelToken& cancel_token = {});

}  // namespace chudware::sim
