// cef/cef_spool_reaper.h — bounded cleanup for Chromium persistent-histogram spools.
// Pure filesystem logic: intentionally no CEF, GL, ImGui, or AppState dependency.
#pragma once

#include <chrono>
#include <cstddef>
#include <filesystem>

namespace ui::cef {

// Chromium's desktop persistent histogram files are normally 4 MiB each. Keep
// at most one hour-scale burst per spool directory while allowing a small set
// of recent prior-process files to survive until the next startup cleanup.
inline constexpr std::size_t kCefMetricsSpoolMaxFilesPerDirectory = 64;

// Chudware never reads or uploads these files. One day is deliberately generous
// to an active/recent file while preventing the observed multi-week buildup.
inline constexpr std::chrono::hours kCefMetricsSpoolMaxAge{24};

struct CefSpoolReapResult {
    std::size_t matched = 0;
    std::size_t removed_by_age = 0;
    std::size_t removed_by_count = 0;
    std::size_t removal_failures = 0;
    std::size_t retained = 0;
};

// Reap only direct, regular "*.pma" children of cache_root/BrowserMetrics and
// cache_root/DeferredBrowserMetrics. Missing/inaccessible directories and files
// concurrently removed or held open are non-fatal. `now` is explicit so tests
// can backdate synthetic files deterministically.
CefSpoolReapResult reap_cef_metrics_spools(
    const std::filesystem::path& cache_root,
    std::chrono::seconds max_age,
    std::size_t max_files_per_directory,
    std::filesystem::file_time_type now =
        std::filesystem::file_time_type::clock::now());

}  // namespace ui::cef
