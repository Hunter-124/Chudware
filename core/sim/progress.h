#pragma once

#include <chrono>
#include <cstddef>
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
}  // namespace progress_detail

inline void set_progress_sink(void (*sink)(const char*, void*), void* user_data) {
    progress_detail::sink = sink;
    progress_detail::user_data = user_data;
    progress_detail::elements.reset();
    progress_detail::probe = nullptr;
    if (sink) progress_detail::started = std::chrono::steady_clock::now();
}

// Omit elements until measured; subsequently omission retains the last real count.
// A probe is published by the fill itself, not inferred from phase boundaries.
inline void report_progress(const char* phase,
                            std::optional<std::size_t> elements = std::nullopt) {
    if (!progress_detail::sink) return;
    if (elements) progress_detail::elements = elements;
    nlohmann::json event = {
        {"type", "sim.progress"},
        {"phase", phase},
        {"elapsed_s", std::chrono::duration<double>(
                          std::chrono::steady_clock::now() - progress_detail::started).count()},
        {"elements", nullptr}};
    if (progress_detail::elements) event["elements"] = *progress_detail::elements;
    if (!progress_detail::probe.is_null()) event["probe"] = progress_detail::probe;
    const auto text = event.dump();
    progress_detail::sink(text.c_str(), progress_detail::user_data);
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

}  // namespace chudware::sim
