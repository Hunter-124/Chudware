// Unit tests for the shared work-queue thread pool (util/thread_pool.h) and its
// governor-bounded max workers. Deterministic: identical inputs => identical
// results. Wall times are recorded and printed (no expected-time assertions).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <future>
#include <string>
#include <thread>
#include <vector>

#include "util/resource_governor.h"
#include "util/thread_pool.h"

using chudware::pool::ThreadPool;

namespace {
void put_env(const char* k, const char* v) {
#if defined(_WIN32)
    _putenv_s(k, v ? v : "");
#else
    if (v) setenv(k, v, 1);
    else unsetenv(k);
#endif
}

double wall_ms(const std::chrono::steady_clock::time_point& t0) {
    return std::chrono::duration<double, std::milli>(
               std::chrono::steady_clock::now() - t0)
        .count();
}
}  // namespace

TEST_CASE("parallel_for sum matches serial (correctness)") {
    const auto t0 = std::chrono::steady_clock::now();
    const int N = 10000;
    std::vector<long long> data(static_cast<std::size_t>(N));
    for (int i = 0; i < N; ++i) data[static_cast<std::size_t>(i)] = i + 1;

    long long serial = 0;
    for (int i = 0; i < N; ++i) serial += data[static_cast<std::size_t>(i)];

    ThreadPool pool(4);
    std::vector<long long> partial(static_cast<std::size_t>(N), 0);
    pool.parallel_for(N, [&](int i) {
        partial[static_cast<std::size_t>(i)] = data[static_cast<std::size_t>(i)];
    });
    long long parallel = 0;
    for (int i = 0; i < N; ++i) parallel += partial[static_cast<std::size_t>(i)];

    CHECK(parallel == serial);
    CHECK(parallel == static_cast<long long>(N) * (N + 1) / 2);
    std::printf("[thread_pool] parallel_for sum=%lld serial=%lld workers=4 wall=%.3f ms\n",
                parallel, serial, wall_ms(t0));
}

TEST_CASE("pool caps workers to governed budget under nested submits") {
    const auto t0 = std::chrono::steady_clock::now();
    // Fixed small pool — nested parallel_for from a worker MUST run inline so
    // we never spawn more threads than max_workers (no oversubscription).
    ThreadPool pool(2);
    CHECK(pool.max_workers() == 2);

    std::atomic<int> outer_in_flight{0};
    std::atomic<int> outer_peak{0};
    std::atomic<int> nested_runs{0};
    std::atomic<int> nested_from_worker{0};

    pool.parallel_for(8, [&](int /*i*/) {
        const int cur = outer_in_flight.fetch_add(1) + 1;
        int prev = outer_peak.load();
        while (cur > prev && !outer_peak.compare_exchange_weak(prev, cur)) {
        }
        // Nested submit from a worker: must run inline (in_worker path) — no
        // extra threads, so outer_peak stays <= max_workers.
        if (pool.in_worker()) {
            nested_from_worker.fetch_add(1);
            pool.parallel_for(4, [&](int /*j*/) {
                nested_runs.fetch_add(1);
                // Prove nested path is still on a pool worker (inline), not a
                // newly spawned thread outside the budget.
                CHECK(pool.in_worker());
            });
        } else {
            // Serial fallback (max_workers==1 path) still runs nested bodies.
            pool.parallel_for(4, [&](int /*j*/) { nested_runs.fetch_add(1); });
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
        outer_in_flight.fetch_sub(1);
    });

    CHECK(outer_peak.load() <= pool.max_workers());
    CHECK(nested_runs.load() == 8 * 4);
    CHECK(nested_from_worker.load() == 8);  // every outer body saw in_worker
    std::printf(
        "[thread_pool] nested outer_peak=%d max_workers=%d nested_runs=%d "
        "from_worker=%d wall=%.3f ms\n",
        outer_peak.load(), pool.max_workers(), nested_runs.load(),
        nested_from_worker.load(), wall_ms(t0));
}

TEST_CASE("reconfigure_shared respects governor plan under low / ample memory") {
    const auto t0 = std::chrono::steady_clock::now();
    put_env("CHUDWARE_GOVERNOR", nullptr);
    put_env("CHUDWARE_SIM_THREADS", "6");

    // Ample: tiny estimate => full allotment.
    const int ample = chudware::pool::reconfigure_shared(1ull << 20, "pool_ample");
    CHECK(ample == 6);
    CHECK(chudware::pool::shared().max_workers() == 6);
    CHECK(chudware::governor::last_report().find("pool_ample") != std::string::npos);

    // Low: estimate far above budget => workers collapse toward 1.
    put_env("CHUDWARE_MEM_CAP_GB", "1");
    const uint64_t budget = chudware::governor::budget_bytes();
    const int tight =
        chudware::pool::reconfigure_shared(budget * 16ull + (1ull << 30), "pool_tight");
    CHECK(tight == 1);
    CHECK(chudware::pool::shared().max_workers() == 1);
    CHECK(chudware::governor::last_report().find("pool_tight") != std::string::npos);

    // Restore a sane shared size for any later tests in the same process.
    put_env("CHUDWARE_MEM_CAP_GB", nullptr);
    put_env("CHUDWARE_SIM_THREADS", "2");
    (void)chudware::pool::reconfigure_shared(0, "pool_restore");
    put_env("CHUDWARE_SIM_THREADS", nullptr);

    std::printf(
        "[thread_pool] reconfigure ample=%d tight=%d final_max=%d wall=%.3f ms\n",
        ample, tight, chudware::pool::shared().max_workers(), wall_ms(t0));
}

TEST_CASE("submit + wait drains the queue") {
    const auto t0 = std::chrono::steady_clock::now();
    ThreadPool pool(3);
    std::atomic<int> hits{0};
    std::vector<std::future<void>> futs;
    futs.reserve(20);
    for (int i = 0; i < 20; ++i) {
        futs.push_back(pool.submit([&] { hits.fetch_add(1); }));
    }
    for (auto& f : futs) f.get();
    pool.wait();
    CHECK(hits.load() == 20);
    std::printf("[thread_pool] submit+wait hits=%d wall=%.3f ms\n", hits.load(),
                wall_ms(t0));
}
