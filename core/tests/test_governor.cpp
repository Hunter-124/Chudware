// Unit tests for the host resource governor (util/resource_governor.h).
// Budgets are recomputed per call, so env overrides can be tested with setenv
// inside a single process. Platform note: on an unknowable platform host_mem()
// returns zeros and the governor admits everything — the SKIP branch keeps the
// test honest there instead of asserting numbers that don't exist.
//
// F3: plan_for grades workers + reserve under synthetic low / ample memory;
// every decision lands in the report string; last_report() mirrors it.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>

#include "util/resource_governor.h"

using namespace aicad::governor;

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

TEST_CASE("host_mem is sane") {
    const auto t0 = std::chrono::steady_clock::now();
    const HostMem m = host_mem();
    if (m.total == 0) {
        std::printf("[governor] host_mem: platform unknown (SKIP) wall=%.3f ms\n",
                    wall_ms(t0));
        return;
    }
    CHECK(m.total > 0);
    CHECK(m.avail <= m.total);
    std::printf("[governor] host_mem total=%.2fGB avail=%.2fGB rss=%.2fGB wall=%.3f ms\n",
                m.total / 1e9, m.avail / 1e9, process_rss_bytes() / 1e9, wall_ms(t0));
}

TEST_CASE("budget within (0, total] and tracks the cap") {
    const auto t0 = std::chrono::steady_clock::now();
    put_env("AICAD_GOVERNOR", nullptr);
    put_env("AICAD_MEM_CAP_GB", nullptr);
    const HostMem m = host_mem();
    if (m.total == 0) {
        std::printf("[governor] budget: platform unknown (SKIP) wall=%.3f ms\n",
                    wall_ms(t0));
        return;
    }
    const uint64_t b = budget_bytes();
    CHECK(b > 0);
    CHECK(b <= m.total);

    put_env("AICAD_MEM_CAP_GB", "2");
    const uint64_t capped = budget_bytes();
    // The floor (min(3GB, 40% total)) may sit above a 2 GB cap on big machines;
    // either way the capped budget can never exceed the uncapped one.
    CHECK(capped <= b);
    put_env("AICAD_MEM_CAP_GB", nullptr);
    std::printf("[governor] budget uncapped=%llu capped2GB=%llu wall=%.3f ms\n",
                (unsigned long long)b, (unsigned long long)capped, wall_ms(t0));
}

TEST_CASE("admit small, refuse absurd, message carries the knobs") {
    const auto t0 = std::chrono::steady_clock::now();
    put_env("AICAD_GOVERNOR", nullptr);
    const HostMem m = host_mem();
    if (m.total == 0) {
        std::printf("[governor] admit: platform unknown (SKIP) wall=%.3f ms\n",
                    wall_ms(t0));
        return;
    }
    std::string why;
    CHECK(admit(64ull * 1024 * 1024, "tiny study", why));  // 64 MB always fits
    why.clear();
    // 64x total RAM can never fit any fraction-based budget.
    CHECK_FALSE(admit(m.total * 64ull, "absurd study", why));
    CHECK(why.find("absurd study") != std::string::npos);
    CHECK(why.find("AICAD_MEM_FRAC") != std::string::npos);
    // Observability: last_report carries the refusal.
    CHECK(last_report().find("absurd study") != std::string::npos);
    std::printf("[governor] admit refuse why_len=%zu wall=%.3f ms\n", why.size(),
                wall_ms(t0));
}

TEST_CASE("AICAD_GOVERNOR=0 admits everything") {
    const auto t0 = std::chrono::steady_clock::now();
    put_env("AICAD_GOVERNOR", "0");
    std::string why;
    CHECK(admit(UINT64_MAX / 2, "monster", why));
    CHECK(budget_bytes() == UINT64_MAX);
    CHECK_FALSE(low_memory_now());
    // plan_for with governor off: full workers, admitted.
    put_env("AI_CAD_SIM_THREADS", "5");
    const Plan p = plan_for(UINT64_MAX / 4, "monster_plan", 0);
    CHECK(p.admitted);
    CHECK(p.workers == 5);
    CHECK(p.report.find("governor off") != std::string::npos);
    put_env("AI_CAD_SIM_THREADS", nullptr);
    put_env("AICAD_GOVERNOR", nullptr);
    std::printf("[governor] disabled workers=%d wall=%.3f ms\n", p.workers,
                wall_ms(t0));
}

TEST_CASE("solver_threads positive and honors the env cap") {
    const auto t0 = std::chrono::steady_clock::now();
    put_env("AI_CAD_SIM_THREADS", nullptr);
    CHECK(solver_threads() >= 1);
    put_env("AI_CAD_SIM_THREADS", "3");
    CHECK(solver_threads() == 3);
    put_env("AI_CAD_SIM_THREADS", nullptr);
    std::printf("[governor] solver_threads default>=1 env=3 wall=%.3f ms\n",
                wall_ms(t0));
}

TEST_CASE("plan_for ample memory returns ~all allotted cores") {
    const auto t0 = std::chrono::steady_clock::now();
    put_env("AICAD_GOVERNOR", nullptr);
    put_env("AICAD_MEM_CAP_GB", nullptr);
    put_env("AI_CAD_SIM_THREADS", "6");
    // Tiny estimate: pressure ~0 => full allotment, no self-throttle.
    const Plan p = plan_for(1ull * 1024 * 1024, "tiny_cpu", 0);
    CHECK(p.workers == 6);
    CHECK(p.admitted);
    if (p.budget != UINT64_MAX) {
        CHECK(p.reserve_bytes <= p.budget);
    }
    CHECK(p.report.find("workers=6") != std::string::npos);
    CHECK(last_report().find("tiny_cpu") != std::string::npos);
    put_env("AI_CAD_SIM_THREADS", nullptr);
    std::printf("[governor] ample plan workers=%d reserve=%llu budget=%llu wall=%.3f ms\n",
                p.workers, (unsigned long long)p.reserve_bytes,
                (unsigned long long)p.budget, wall_ms(t0));
}

TEST_CASE("plan_for low-memory reduces workers and never reserves past headroom") {
    const auto t0 = std::chrono::steady_clock::now();
    put_env("AICAD_GOVERNOR", nullptr);
    put_env("AI_CAD_SIM_THREADS", "8");
    // Force a tight budget via absolute cap. Floor may lift the budget on big
    // boxes, so drive pressure with an estimate far above budget.
    put_env("AICAD_MEM_CAP_GB", "1");
    put_env("AICAD_HEADROOM_GB", "4");
    const HostMem m = host_mem();
    if (m.total == 0) {
        put_env("AICAD_MEM_CAP_GB", nullptr);
        put_env("AICAD_HEADROOM_GB", nullptr);
        put_env("AI_CAD_SIM_THREADS", nullptr);
        std::printf("[governor] low-mem plan: platform unknown (SKIP) wall=%.3f ms\n",
                    wall_ms(t0));
        return;
    }
    const uint64_t budget = budget_bytes();
    // Estimate 8x budget => pressure >= 1 => workers must drop to 1.
    const uint64_t est = budget * 8ull + (1ull << 30);
    const Plan p = plan_for(est, "tight_study", 0);
    CHECK(p.workers == 1);
    CHECK_FALSE(p.admitted);
    // Reservation never exceeds the budget (and never the free envelope when known).
    CHECK(p.reserve_bytes <= p.budget);
    if (m.avail > 0) {
        const uint64_t headroom = 4ull * 1024ull * 1024ull * 1024ull;
        const uint64_t envelope = (m.avail > headroom) ? (m.avail - headroom) : 0;
        if (envelope > 0) CHECK(p.reserve_bytes <= envelope);
    }
    CHECK(p.report.find("tight_study") != std::string::npos);
    CHECK(p.report.find("workers=1") != std::string::npos);
    std::printf(
        "[governor] low-mem plan workers=%d reserve=%.3fGB budget=%.3fGB "
        "admitted=%d wall=%.3f ms\n",
        p.workers, p.reserve_bytes / 1e9, p.budget / 1e9, p.admitted ? 1 : 0,
        wall_ms(t0));
    put_env("AICAD_MEM_CAP_GB", nullptr);
    put_env("AICAD_HEADROOM_GB", nullptr);
    put_env("AI_CAD_SIM_THREADS", nullptr);
}

TEST_CASE("plan_for mid pressure scales workers between 1 and allotment") {
    const auto t0 = std::chrono::steady_clock::now();
    put_env("AICAD_GOVERNOR", nullptr);
    put_env("AICAD_MEM_CAP_GB", nullptr);
    put_env("AI_CAD_SIM_THREADS", "8");
    const HostMem m = host_mem();
    if (m.total == 0) {
        put_env("AI_CAD_SIM_THREADS", nullptr);
        std::printf("[governor] mid-pressure: platform unknown (SKIP) wall=%.3f ms\n",
                    wall_ms(t0));
        return;
    }
    const uint64_t budget = budget_bytes();
    // ~50% of budget => pressure ~0.5 => workers between 1 and 8 exclusive of both ends
    // (unless allot is 1).
    const Plan p = plan_for(budget / 2, "mid_study", 0);
    CHECK(p.workers >= 1);
    CHECK(p.workers <= 8);
    CHECK(p.admitted);
    CHECK(p.reserve_bytes <= p.budget);
    std::printf("[governor] mid-pressure workers=%d/8 reserve=%.3fGB wall=%.3f ms\n",
                p.workers, p.reserve_bytes / 1e9, wall_ms(t0));
    put_env("AI_CAD_SIM_THREADS", nullptr);
}
