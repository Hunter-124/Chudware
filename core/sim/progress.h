#pragma once

#include <chrono>
#include <cmath>
#include <cstddef>
#include <limits>
#include <optional>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware::sim {

// The existing C event hook is armed on the same thread as the simulation RPC.
// No callback means no clock reads, JSON construction, or per-phase allocations.
namespace progress_detail {
inline thread_local void (*sink)(const char*, void*) = nullptr;
inline thread_local void* user_data = nullptr;
inline thread_local std::optional<std::size_t> elements;
inline thread_local std::chrono::steady_clock::time_point started;
inline thread_local nlohmann::json probe;

// The base sim.progress event: {type, phase, elapsed_s, elements, probe?}. Every
// emitter builds on it, so extensions only ever ADD keys to this shape.
inline nlohmann::json event(const char* phase, std::optional<std::size_t> count) {
    if (count) elements = count;
    nlohmann::json out = {
        {"type", "sim.progress"},
        {"phase", phase},
        {"elapsed_s", std::chrono::duration<double>(
                          std::chrono::steady_clock::now() - started).count()},
        {"elements", nullptr}};
    if (elements) out["elements"] = *elements;
    if (!probe.is_null()) out["probe"] = probe;
    return out;
}

inline void emit(const nlohmann::json& out) {
    const auto text = out.dump();
    sink(text.c_str(), user_data);
}
}  // namespace progress_detail

inline void set_progress_sink(void (*sink)(const char*, void*), void* user_data) {
    progress_detail::sink = sink;
    progress_detail::user_data = user_data;
    progress_detail::elements.reset();
    progress_detail::probe = nullptr;
    if (sink) progress_detail::started = std::chrono::steady_clock::now();
}

// True while an event sink is armed on this thread. Callers that would build
// metrics only for progress (per-constraint tables) check this first.
inline bool progress_enabled() { return progress_detail::sink != nullptr; }

// Omit elements until measured; subsequently omission retains the last real count.
// A probe is published by the fill itself, not inferred from phase boundaries.
inline void report_progress(const char* phase,
                            std::optional<std::size_t> elements = std::nullopt) {
    if (!progress_detail::sink) return;
    progress_detail::emit(progress_detail::event(phase, elements));
}

inline void report_mesh_progress(const char* phase, double h_mm,
                                 std::size_t elements_so_far, std::size_t cells_done,
                                 std::size_t cells_total, const std::string& sub_phase) {
    if (!progress_detail::sink) return;
    progress_detail::probe = {{"h_mm", h_mm}, {"elements_so_far", elements_so_far},
                              {"cells_done", cells_done}, {"cells_total", cells_total},
                              {"sub_phase", sub_phase}};
    report_progress(phase, elements_so_far);
}

// ── Iterative-solver progress ────────────────────────────────────────────────
// One iteration of an iterative study (an optimizer, a continuation), published as
// the ordinary sim.progress event plus an `iteration` object:
//   iteration: {index, max, objective, objective_change, volume_fraction,
//               design_change, max_constraint, constraints?, metrics?}
// Scalars left NaN (not measured this iteration) are emitted as null, never as a
// plausible-looking zero; `max` is null when max_iterations <= 0. `constraints` is
// an optional object {name: g} in the g <= 0 (feasible) convention and
// `max_constraint` its maximum; `metrics` an optional object of further named
// physical values with their units in the name (e.g. "compliance_n_mm").
struct IterationProgress {
    int index = 0;
    int max_iterations = 0;
    double objective = std::numeric_limits<double>::quiet_NaN();
    double objective_change = std::numeric_limits<double>::quiet_NaN();  // relative
    double volume_fraction = std::numeric_limits<double>::quiet_NaN();
    double design_change = std::numeric_limits<double>::quiet_NaN();     // max |Δρ|
    double max_constraint = std::numeric_limits<double>::quiet_NaN();
    nlohmann::json constraints;  // null or {name: g}
    nlohmann::json metrics;      // null or {name: value}
};

inline void report_iteration_progress(const char* phase, const IterationProgress& it,
                                      std::optional<std::size_t> elements = std::nullopt) {
    if (!progress_detail::sink) return;
    auto measured = [](double v) {
        return std::isfinite(v) ? nlohmann::json(v) : nlohmann::json(nullptr);
    };
    nlohmann::json iteration = {
        {"index", it.index},
        {"max", it.max_iterations > 0 ? nlohmann::json(it.max_iterations)
                                      : nlohmann::json(nullptr)},
        {"objective", measured(it.objective)},
        {"objective_change", measured(it.objective_change)},
        {"volume_fraction", measured(it.volume_fraction)},
        {"design_change", measured(it.design_change)},
        {"max_constraint", measured(it.max_constraint)}};
    if (it.constraints.is_object() && !it.constraints.empty())
        iteration["constraints"] = it.constraints;
    if (it.metrics.is_object() && !it.metrics.empty()) iteration["metrics"] = it.metrics;
    nlohmann::json out = progress_detail::event(phase, elements);
    out["iteration"] = std::move(iteration);
    progress_detail::emit(out);
}

}  // namespace chudware::sim
