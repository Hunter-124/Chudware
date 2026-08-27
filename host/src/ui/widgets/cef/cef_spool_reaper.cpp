// cef/cef_spool_reaper.cpp — see cef_spool_reaper.h.
#include "cef_spool_reaper.h"

#include <algorithm>
#include <array>
#include <system_error>
#include <vector>

namespace ui::cef {
namespace {

constexpr std::array<const char*, 2> kMetricsSpoolDirectories = {
    "BrowserMetrics",
    "DeferredBrowserMetrics",
};

struct Candidate {
    std::filesystem::path path;
    std::filesystem::file_time_type modified;
    bool removed = false;
};

void reap_directory(const std::filesystem::path& directory,
                    std::chrono::seconds max_age,
                    std::size_t max_files,
                    std::filesystem::file_time_type now,
                    CefSpoolReapResult& result) {
    std::error_code iter_error;
    std::filesystem::directory_iterator iter(
        directory, std::filesystem::directory_options::skip_permission_denied, iter_error);
    if (iter_error) return;  // Missing/inaccessible spools must not block CEF startup.

    std::vector<Candidate> candidates;
    const auto cutoff = now - std::chrono::duration_cast<
                                  std::filesystem::file_time_type::duration>(max_age);
    const std::filesystem::directory_iterator end;
    while (iter != end) {
        const std::filesystem::directory_entry entry = *iter;
        std::error_code entry_error;
        const auto status = entry.symlink_status(entry_error);
        if (!entry_error && std::filesystem::is_regular_file(status) &&
            entry.path().extension() == ".pma") {
            const auto modified = entry.last_write_time(entry_error);
            if (!entry_error) candidates.push_back({entry.path(), modified, false});
        }

        iter.increment(iter_error);
        if (iter_error) break;  // Concurrent mutation or an I/O error is non-fatal.
    }

    result.matched += candidates.size();

    // Age is the primary bound. A failed remove (for example, an active Windows
    // mapping) remains a survivor and is harmless; startup must never fail for cleanup.
    for (auto& candidate : candidates) {
        if (candidate.modified >= cutoff) continue;
        std::error_code remove_error;
        if (std::filesystem::remove(candidate.path, remove_error)) {
            candidate.removed = true;
            ++result.removed_by_age;
        } else if (remove_error) {
            ++result.removal_failures;
        }
    }

    std::vector<Candidate*> survivors;
    survivors.reserve(candidates.size());
    for (auto& candidate : candidates) {
        if (!candidate.removed) survivors.push_back(&candidate);
    }
    std::sort(survivors.begin(), survivors.end(), [](const Candidate* a, const Candidate* b) {
        if (a->modified != b->modified) return a->modified > b->modified;
        return a->path.native() < b->path.native();
    });

    // Retain the newest files. The count bound also handles a pathological
    // restart loop that creates many files before any one of them reaches max_age.
    for (std::size_t i = max_files; i < survivors.size(); ++i) {
        std::error_code remove_error;
        if (std::filesystem::remove(survivors[i]->path, remove_error)) {
            survivors[i]->removed = true;
            ++result.removed_by_count;
        } else if (remove_error) {
            ++result.removal_failures;
        }
    }
}

}  // namespace

CefSpoolReapResult reap_cef_metrics_spools(
    const std::filesystem::path& cache_root,
    std::chrono::seconds max_age,
    std::size_t max_files_per_directory,
    std::filesystem::file_time_type now) {
    CefSpoolReapResult result;
    for (const char* leaf : kMetricsSpoolDirectories) {
        reap_directory(cache_root / leaf, max_age, max_files_per_directory, now, result);
    }
    result.retained = result.matched - result.removed_by_age - result.removed_by_count;
    return result;
}

}  // namespace ui::cef
