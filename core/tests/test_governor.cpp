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

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <system_error>
#include <thread>
#include <utility>

#include "environment_test_support.h"
#include "util/resource_governor.h"

using namespace chudware::governor;

namespace {

double wall_ms(const std::chrono::steady_clock::time_point& t0) {
    return std::chrono::duration<double, std::milli>(
               std::chrono::steady_clock::now() - t0)
        .count();
}

// A directory of fake cgroup v2 control files, handed to the governor through
// CHUDWARE_CGROUP_ROOT (which also short-circuits the /proc/self/cgroup lookup,
// so the fake dir is the whole hierarchy). Lets the cgroup budget term be pinned
// on a box that is not itself in a capped cgroup, and vice versa.
struct FakeCgroup {
    std::filesystem::path dir;

    explicit FakeCgroup(const char* name) {
        dir = std::filesystem::temp_directory_path() / "chudware_governor_cgroup" / name;
        std::error_code ec;
        std::filesystem::remove_all(dir, ec);
        std::filesystem::create_directories(dir, ec);
    }
    ~FakeCgroup() {
        std::error_code ec;
        std::filesystem::remove_all(dir, ec);
    }
    FakeCgroup(const FakeCgroup&) = delete;
    FakeCgroup& operator=(const FakeCgroup&) = delete;

    void put(const char* file, const char* text) const {
        if (FILE* f = std::fopen((dir / file).string().c_str(), "w")) {
            std::fputs(text, f);
            std::fclose(f);
        }
    }
    // Budget with this fake cgroup in force.
    uint64_t budget() const {
        chudware::test::setOrUnsetEnvironment("CHUDWARE_CGROUP_ROOT",
                                              dir.string().c_str());
        const uint64_t b = budget_bytes();
        chudware::test::setOrUnsetEnvironment("CHUDWARE_CGROUP_ROOT", nullptr);
        return b;
    }
};

constexpr uint64_t kGiB = 1024ull * 1024ull * 1024ull;
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
    chudware::test::setOrUnsetEnvironment("CHUDWARE_GOVERNOR", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", nullptr);
    const HostMem m = host_mem();
    if (m.total == 0) {
        std::printf("[governor] budget: platform unknown (SKIP) wall=%.3f ms\n",
                    wall_ms(t0));
        return;
    }
    const uint64_t b = budget_bytes();
    CHECK(b > 0);
    CHECK(b <= m.total);

    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", "2");
    const uint64_t capped = budget_bytes();
    // The floor (min(3GB, 40% total)) may sit above a 2 GB cap on big machines;
    // either way the capped budget can never exceed the uncapped one.
    CHECK(capped <= b);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", nullptr);
    std::printf("[governor] budget uncapped=%llu capped2GB=%llu wall=%.3f ms\n",
                (unsigned long long)b, (unsigned long long)capped, wall_ms(t0));
}

TEST_CASE("admit small, refuse absurd, message carries the knobs") {
    const auto t0 = std::chrono::steady_clock::now();
    chudware::test::setOrUnsetEnvironment("CHUDWARE_GOVERNOR", nullptr);
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
    CHECK(why.find("CHUDWARE_MEM_FRAC") != std::string::npos);
    // Observability: last_report carries the refusal.
    CHECK(last_report().find("absurd study") != std::string::npos);
    std::printf("[governor] admit refuse why_len=%zu wall=%.3f ms\n", why.size(),
                wall_ms(t0));
}

TEST_CASE("CHUDWARE_GOVERNOR=0 admits everything") {
    const auto t0 = std::chrono::steady_clock::now();
    chudware::test::setOrUnsetEnvironment("CHUDWARE_GOVERNOR", "0");
    std::string why;
    CHECK(admit(UINT64_MAX / 2, "monster", why));
    CHECK(budget_bytes() == UINT64_MAX);
    CHECK_FALSE(low_memory_now());
    // plan_for with governor off: full workers, admitted.
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", "5");
    const Plan p = plan_for(UINT64_MAX / 4, "monster_plan", 0);
    CHECK(p.admitted);
    CHECK(p.workers == 5);
    CHECK(p.report.find("governor off") != std::string::npos);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_GOVERNOR", nullptr);
    std::printf("[governor] disabled workers=%d wall=%.3f ms\n", p.workers,
                wall_ms(t0));
}

TEST_CASE("solver_threads positive and honors the env cap") {
    const auto t0 = std::chrono::steady_clock::now();
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", nullptr);
    CHECK(solver_threads() >= 1);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", "3");
    CHECK(solver_threads() == 3);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", nullptr);
    std::printf("[governor] solver_threads default>=1 env=3 wall=%.3f ms\n",
                wall_ms(t0));
}

TEST_CASE("plan_for ample memory returns ~all allotted cores") {
    const auto t0 = std::chrono::steady_clock::now();
    chudware::test::setOrUnsetEnvironment("CHUDWARE_GOVERNOR", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", "6");
    // Tiny estimate: pressure ~0 => full allotment, no self-throttle.
    const Plan p = plan_for(1ull * 1024 * 1024, "tiny_cpu", 0);
    CHECK(p.workers == 6);
    CHECK(p.admitted);
    if (p.budget != UINT64_MAX) {
        CHECK(p.reserve_bytes <= p.budget);
    }
    CHECK(p.report.find("workers=6") != std::string::npos);
    CHECK(last_report().find("tiny_cpu") != std::string::npos);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", nullptr);
    std::printf("[governor] ample plan workers=%d reserve=%llu budget=%llu wall=%.3f ms\n",
                p.workers, (unsigned long long)p.reserve_bytes,
                (unsigned long long)p.budget, wall_ms(t0));
}

TEST_CASE("plan_for low-memory reduces workers and never reserves past headroom") {
    const auto t0 = std::chrono::steady_clock::now();
    chudware::test::setOrUnsetEnvironment("CHUDWARE_GOVERNOR", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", "8");
    // Force a tight budget via absolute cap. Floor may lift the budget on big
    // boxes, so drive pressure with an estimate far above budget.
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", "1");
    chudware::test::setOrUnsetEnvironment("CHUDWARE_HEADROOM_GB", "4");
    const HostMem m = host_mem();
    if (m.total == 0) {
        chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", nullptr);
        chudware::test::setOrUnsetEnvironment("CHUDWARE_HEADROOM_GB", nullptr);
        chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", nullptr);
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
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_HEADROOM_GB", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", nullptr);
}

TEST_CASE("plan_for mid pressure scales workers between 1 and allotment") {
    const auto t0 = std::chrono::steady_clock::now();
    chudware::test::setOrUnsetEnvironment("CHUDWARE_GOVERNOR", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", "8");
    const HostMem m = host_mem();
    if (m.total == 0) {
        chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", nullptr);
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
    chudware::test::setOrUnsetEnvironment("CHUDWARE_SIM_THREADS", nullptr);
}

// ── The cgroup budget term ──────────────────────────────────────────────────────
// Regression for a live refusal on a tube-frame study: inside a
// `systemd-run --scope -p MemoryMax=10G` holding the driver process, its python
// worker and chudware-hostd, the term was computed as
// `limit - (memory.current - own_rss)`. memory.current also charges the PAGE
// CACHE (that scope had streamed STEP/VTU files), so it sat within a few hundred
// MB of the limit and the budget collapsed to 0.4 GB — run_fea_static refused a
// study the scope had 8+ GB of anonymous room for, twice. The term now reads
// memory.stat `anon` + `shmem`, credits our own ANON rss back, and keeps the
// small-job floor unless the limit itself is below it.
TEST_CASE("cgroup budget term counts anon usage, not page cache") {
    const auto t0 = std::chrono::steady_clock::now();
#if defined(_WIN32) || defined(__APPLE__)
    std::printf("[governor] cgroup term: not a cgroup platform (SKIP) wall=%.3f ms\n",
                wall_ms(t0));
#else
    const auto previous_probe = set_host_memory_probe(
        +[] { return HostMem{32 * kGiB, 24 * kGiB}; });
    struct RestoreProbe {
        HostMemoryProbe previous;
        ~RestoreProbe() { set_host_memory_probe(previous); }
    } restore_probe{previous_probe};
    chudware::test::setOrUnsetEnvironment("CHUDWARE_GOVERNOR", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_CAP_GB", nullptr);
    const HostMem m = host_mem();
    // Open the host terms as wide as this machine allows, so whatever binds below
    // them is the cgroup term and the pins do not depend on the box's free RAM.
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_FRAC", "1.0");
    chudware::test::setOrUnsetEnvironment("CHUDWARE_HEADROOM_GB", "0");
    const uint64_t floor_b =
        std::min(3 * kGiB, static_cast<uint64_t>(static_cast<double>(m.total) * 0.4));

    // (a) No control files at all: uncapped, the baseline every case compares to.
    const FakeCgroup none("uncapped");
    const uint64_t base = none.budget();
    CHECK(base == 24 * kGiB);

    // (b) Files present but no finite limit: memory.high "max" must not win the
    // min, and a huge memory.current/anon must not shrink anything.
    const FakeCgroup nolimit("nolimit");
    nolimit.put("memory.max", "max\n");
    nolimit.put("memory.high", "max\n");
    nolimit.put("memory.current", "9500000000\n");
    nolimit.put("memory.stat", "anon 9000000000\nfile 400000000\nshmem 0\n");
    CHECK(nolimit.budget() == base);

    // (c) The mission's own shape: 10 GiB limit, siblings holding 1.2 GiB anon +
    // 64 MiB shmem, and a memory.current pinned near the limit by the page cache.
    // The budget must be ~8.74 GiB (limit - anon - shmem, plus our own anon rss
    // credited back), never ~0.5 GiB, and never above the limit.
    const FakeCgroup capped("capped10g");
    capped.put("memory.max", "10737418240\n");
    capped.put("memory.high", "max\n");
    capped.put("memory.current", "10200547328\n");  // 9.5 GiB — mostly page cache
    capped.put("memory.stat",
               "anon 1288490188\nfile 8858370048\nkernel 53673984\nshmem 67108864\n"
               "file_mapped 1048576\ninactive_anon 0\nactive_anon 1288490188\n");
    const uint64_t claim_lo = 10 * kGiB - 1288490188ull - 67108864ull;  // ~8.74 GiB
    const uint64_t claim_hi = claim_lo + 256ull * 1024 * 1024;  // + our own anon rss
    const uint64_t got = capped.budget();
    CHECK(got >= std::min(base, claim_lo));
    CHECK(got <= std::min(base, claim_hi));
    CHECK(got < 10 * kGiB);

    // (d) Siblings really do own almost the whole limit: the subtraction is an
    // estimate of a moving target, so it may not push the budget under the
    // small-job floor — but it may not lift it above the kill line either.
    const FakeCgroup squeezed("squeezed");
    squeezed.put("memory.max", "4294967296\n");  // 4 GiB
    squeezed.put("memory.stat", "anon 4187593113\nfile 0\nshmem 0\n");
    CHECK(squeezed.budget() == std::min(base, std::min(floor_b, 4 * kGiB)));

    // (e) A limit below the floor is the one case with no rescue: a 256 MiB
    // container gets a 256 MiB budget, because crossing it is a SIGKILL.
    const FakeCgroup tiny("tiny");
    tiny.put("memory.max", "268435456\n");
    CHECK(tiny.budget() == 268435456ull);

    // (f) The refusal has to say how much of the limit the siblings hold —
    // otherwise a 10 GB scope reporting a small budget reads as a governor bug.
    if (got < base) {
        chudware::test::setOrUnsetEnvironment("CHUDWARE_CGROUP_ROOT",
                                              capped.dir.string().c_str());
        std::string why;
        CHECK_FALSE(admit(got * 2ull, "cgroup study", why));
        chudware::test::setOrUnsetEnvironment("CHUDWARE_CGROUP_ROOT", nullptr);
        CHECK(why.find("cgroup memory limit 10.0 GB") != std::string::npos);
        CHECK(why.find("other processes sharing it hold") != std::string::npos);
        std::printf("[governor] cgroup refusal: %s\n", why.c_str());
    }

    std::printf("[governor] cgroup base=%.2fGB capped10g=%.2fGB squeezed=%.2fGB "
                "tiny=%.2fGB floor=%.2fGB wall=%.3f ms\n",
                base / 1e9, got / 1e9, squeezed.budget() / 1e9, tiny.budget() / 1e9,
                floor_b / 1e9, wall_ms(t0));
    chudware::test::setOrUnsetEnvironment("CHUDWARE_MEM_FRAC", nullptr);
    chudware::test::setOrUnsetEnvironment("CHUDWARE_HEADROOM_GB", nullptr);
#endif
}

TEST_CASE("thin-slice count forecasts remeasure beyond one octave") {
    // Fixed through-thickness layering: refining the in-plane grid multiplies
    // tet count as h^-2, not h^-3. The second measurement must displace a
    // volumetric prior instead of perpetuating its eightfold count growth.
    MeshCountModel model;
    model.exponent = 3.0;
    model.observe(8.0, 12000.0);
    model.observe(5.68, 12000.0 * std::pow(8.0 / 5.68, 2));
    for (double h : {4.0, 2.84}) {
        const double measured = 12000.0 * std::pow(8.0 / h, 2);
        const double estimate = model.predict(h);
        CHECK(estimate >= measured * 0.5);
        CHECK(estimate <= measured * 2.0);
        model.observe(h, measured);
    }
    CHECK(std::isnan(model.predict(1.0)));
    model.observe(1.42, 12000.0 * std::pow(8.0 / 1.42, 2));
    CHECK(model.predict(1.0) == doctest::Approx(768000.0));
}

TEST_CASE("measured wheel slice forecasts retain a nonmonotone count trend") {
    // CD-210-01C, native mesh-only census: the 4 mm fill is CHEAPER than 5.68.
    // Keeping the bbox h^-2 exponent predicts 621k there versus 227k measured.
    MeshCountModel model;
    model.observe(8.0, 211191.0);
    for (const auto& probe :
         {std::pair{5.68, 308370.0}, std::pair{4.0, 227239.0}}) {
        const double estimate = model.predict(probe.first);
        CHECK(estimate >= probe.second * 0.5);
        CHECK(estimate <= probe.second * 2.0);
        std::printf("[governor] wheel h=%.2f predicted=%.0f measured=%.0f ratio=%.6f\n",
                    probe.first, estimate, probe.second, estimate / probe.second);
        model.observe(probe.first, probe.second);
    }
    CHECK(model.predict(2.84) < model.elems);
}
