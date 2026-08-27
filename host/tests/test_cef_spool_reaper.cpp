// test_cef_spool_reaper.cpp — exercises the shipped GL/CEF-free cleanup logic.
#include "ui/widgets/cef/cef_spool_reaper.h"

#include <cstdio>
#include <fstream>
#include <string>

namespace fs = std::filesystem;
using ui::cef::CefSpoolReapResult;

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

#define CHECK_EQ(a, b)                                                           \
    do {                                                                         \
        const auto _a = (a);                                                     \
        const auto _b = (b);                                                     \
        if (!(_a == _b)) {                                                       \
            std::printf("FAIL %s:%d: %s == %s\n", __FILE__, __LINE__, #a, #b);   \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

class TempTree {
public:
    explicit TempTree(const char* label) {
        static unsigned sequence = 0;
        // libc++ may expose file_clock::duration::rep as a wider implementation
        // type with no std::to_string overload. This is only a per-process temp
        // name; narrowing the tick count plus the monotonic sequence is sufficient
        // and keeps the fixture portable across libstdc++ and libc++.
        const auto stamp = static_cast<long long>(
            fs::file_time_type::clock::now().time_since_epoch().count());
        root = fs::temp_directory_path() /
               (std::string("chudware-cef-reaper-") + label + "-" +
                std::to_string(stamp) + "-" + std::to_string(++sequence));
        std::error_code error;
        fs::create_directories(root, error);
        CHECK(!error);
    }

    ~TempTree() {
        std::error_code ignored;
        fs::remove_all(root, ignored);
    }

    fs::path root;
};

static void make_file(const fs::path& path, fs::file_time_type modified) {
    std::error_code error;
    fs::create_directories(path.parent_path(), error);
    CHECK(!error);
    std::ofstream stream(path, std::ios::binary);
    stream.put('\0');
    stream.close();
    fs::last_write_time(path, modified, error);
    CHECK(!error);
}

static std::size_t count_direct_pma(const fs::path& directory) {
    std::error_code error;
    fs::directory_iterator iter(directory, error);
    if (error) return 0;
    std::size_t count = 0;
    for (const auto& entry : iter) {
        const auto status = entry.symlink_status(error);
        if (!error && fs::is_regular_file(status) && entry.path().extension() == ".pma")
            ++count;
        error.clear();
    }
    return count;
}

static void test_missing_directories_are_safe() {
    TempTree tree("missing");
    const CefSpoolReapResult result = ui::cef::reap_cef_metrics_spools(
        tree.root / "does-not-exist", std::chrono::hours(24), 2);
    CHECK_EQ(result.matched, std::size_t{0});
    CHECK_EQ(result.retained, std::size_t{0});
    CHECK_EQ(result.removal_failures, std::size_t{0});
}

static void test_exact_scope_age_and_count_survivors() {
    TempTree tree("scope");
    const auto now = fs::file_time_type::clock::now();
    const auto old = now - std::chrono::hours(49);
    const auto recent = now - std::chrono::hours(1);

    const fs::path browser = tree.root / "BrowserMetrics";
    make_file(browser / "old-a.pma", old);
    make_file(browser / "old-b.pma", old + std::chrono::minutes(1));
    make_file(browser / "recent-a.pma", recent - std::chrono::minutes(2));
    make_file(browser / "recent-b.pma", recent - std::chrono::minutes(1));
    make_file(browser / "recent-c.pma", recent);

    const fs::path deferred = tree.root / "DeferredBrowserMetrics";
    make_file(deferred / "old.pma", old);
    make_file(deferred / "recent-a.pma", recent - std::chrono::minutes(1));
    make_file(deferred / "recent-b.pma", recent);

    // Old or similarly named content outside the exact two direct spool dirs is not ours.
    make_file(browser / "keep.txt", old);
    make_file(browser / "keep.PMA", old);
    make_file(browser / "nested" / "keep.pma", old);
    make_file(tree.root / "OtherMetrics" / "keep.pma", old);
    std::error_code error;
    fs::create_directory(browser / "directory.pma", error);
    CHECK(!error);

    const CefSpoolReapResult result = ui::cef::reap_cef_metrics_spools(
        tree.root, std::chrono::hours(24), 2, now);

    CHECK_EQ(result.matched, std::size_t{8});
    CHECK_EQ(result.removed_by_age, std::size_t{3});
    CHECK_EQ(result.removed_by_count, std::size_t{1});
    CHECK_EQ(result.removal_failures, std::size_t{0});
    CHECK_EQ(result.retained, std::size_t{4});

    CHECK(!fs::exists(browser / "old-a.pma"));
    CHECK(!fs::exists(browser / "old-b.pma"));
    CHECK(!fs::exists(browser / "recent-a.pma"));
    CHECK(fs::exists(browser / "recent-b.pma"));
    CHECK(fs::exists(browser / "recent-c.pma"));
    CHECK(!fs::exists(deferred / "old.pma"));
    CHECK(fs::exists(deferred / "recent-a.pma"));
    CHECK(fs::exists(deferred / "recent-b.pma"));

    CHECK(fs::exists(browser / "keep.txt"));
    CHECK(fs::exists(browser / "keep.PMA"));
    CHECK(fs::exists(browser / "nested" / "keep.pma"));
    CHECK(fs::exists(browser / "directory.pma"));
    CHECK(fs::exists(tree.root / "OtherMetrics" / "keep.pma"));
}

static void populate_uniform_16_days(const fs::path& directory,
                                     std::size_t count,
                                     fs::file_time_type now) {
    const auto span = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::hours(16 * 24));
    for (std::size_t i = 0; i < count; ++i) {
        using SpanRep = decltype(span)::rep;
        const auto age =
            count > 1
                ? span * static_cast<SpanRep>(i) / static_cast<SpanRep>(count - 1)
                : decltype(span)::zero();
        make_file(directory / ("BrowserMetrics-" + std::to_string(i) + ".pma"),
                  now - age);
    }
}

static void test_measured_16_day_accumulation() {
    TempTree daemon("sim-daemon");
    TempTree gui("sim-gui");
    const auto now = fs::file_time_type::clock::now();

    populate_uniform_16_days(daemon.root / "BrowserMetrics", 1891, now);
    populate_uniform_16_days(daemon.root / "DeferredBrowserMetrics", 665, now);
    populate_uniform_16_days(gui.root / "BrowserMetrics", 234, now);

    const std::size_t before = count_direct_pma(daemon.root / "BrowserMetrics") +
                               count_direct_pma(daemon.root / "DeferredBrowserMetrics") +
                               count_direct_pma(gui.root / "BrowserMetrics");
    CHECK_EQ(before, std::size_t{2790});  // Old behavior: every one survived indefinitely.

    const auto daemon_result = ui::cef::reap_cef_metrics_spools(
        daemon.root, ui::cef::kCefMetricsSpoolMaxAge,
        ui::cef::kCefMetricsSpoolMaxFilesPerDirectory, now);
    const auto gui_result = ui::cef::reap_cef_metrics_spools(
        gui.root, ui::cef::kCefMetricsSpoolMaxAge,
        ui::cef::kCefMetricsSpoolMaxFilesPerDirectory, now);
    const std::size_t daemon_browser_after =
        count_direct_pma(daemon.root / "BrowserMetrics");
    const std::size_t daemon_deferred_after =
        count_direct_pma(daemon.root / "DeferredBrowserMetrics");
    const std::size_t gui_browser_after = count_direct_pma(gui.root / "BrowserMetrics");
    const std::size_t after =
        daemon_browser_after + daemon_deferred_after + gui_browser_after;

    CHECK_EQ(after, std::size_t{121});
    CHECK_EQ(daemon_result.removal_failures + gui_result.removal_failures,
             std::size_t{0});
    CHECK(after <= 3 * ui::cef::kCefMetricsSpoolMaxFilesPerDirectory);
    std::printf(
        "simulated_16_day_accumulation: before=%zu after=%zu removed=%zu "
        "daemon_browser=%zu daemon_deferred=%zu gui_browser=%zu\n",
        before, after, before - after, daemon_browser_after, daemon_deferred_after,
        gui_browser_after);
}

int main() {
    test_missing_directories_are_safe();
    test_exact_scope_age_and_count_survivors();
    test_measured_16_day_accumulation();
    if (g_failures) {
        std::printf("test_cef_spool_reaper: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_cef_spool_reaper: all checks passed\n");
    return 0;
}
