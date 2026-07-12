// Unit tests for the host resource governor (util/resource_governor.h).
// Budgets are recomputed per call, so env overrides can be tested with setenv
// inside a single process. Platform note: on an unknowable platform host_mem()
// returns zeros and the governor admits everything — the SKIP branch keeps the
// test honest there instead of asserting numbers that don't exist.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cstdlib>
#include <string>

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
}  // namespace

TEST_CASE("host_mem is sane") {
    const HostMem m = host_mem();
    if (m.total == 0) return;  // unknowable platform: rest of the suite still runs
    CHECK(m.total > 0);
    CHECK(m.avail <= m.total);
}

TEST_CASE("budget within (0, total] and tracks the cap") {
    put_env("AICAD_GOVERNOR", nullptr);
    put_env("AICAD_MEM_CAP_GB", nullptr);
    const HostMem m = host_mem();
    if (m.total == 0) return;
    const uint64_t b = budget_bytes();
    CHECK(b > 0);
    CHECK(b <= m.total);

    put_env("AICAD_MEM_CAP_GB", "2");
    const uint64_t capped = budget_bytes();
    // The floor (min(3GB, 40% total)) may sit above a 2 GB cap on big machines;
    // either way the capped budget can never exceed the uncapped one.
    CHECK(capped <= b);
    put_env("AICAD_MEM_CAP_GB", nullptr);
}

TEST_CASE("admit small, refuse absurd, message carries the knobs") {
    put_env("AICAD_GOVERNOR", nullptr);
    const HostMem m = host_mem();
    if (m.total == 0) return;
    std::string why;
    CHECK(admit(64ull * 1024 * 1024, "tiny study", why));  // 64 MB always fits
    why.clear();
    // 64x total RAM can never fit any fraction-based budget.
    CHECK_FALSE(admit(m.total * 64ull, "absurd study", why));
    CHECK(why.find("absurd study") != std::string::npos);
    CHECK(why.find("AICAD_MEM_FRAC") != std::string::npos);
}

TEST_CASE("AICAD_GOVERNOR=0 admits everything") {
    put_env("AICAD_GOVERNOR", "0");
    std::string why;
    CHECK(admit(UINT64_MAX / 2, "monster", why));
    CHECK(budget_bytes() == UINT64_MAX);
    CHECK_FALSE(low_memory_now());
    put_env("AICAD_GOVERNOR", nullptr);
}

TEST_CASE("solver_threads positive and honors the env cap") {
    put_env("AI_CAD_SIM_THREADS", nullptr);
    CHECK(solver_threads() >= 1);
    put_env("AI_CAD_SIM_THREADS", "3");
    CHECK(solver_threads() == 3);
    put_env("AI_CAD_SIM_THREADS", nullptr);
}
