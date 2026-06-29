// Unit test for the in-process sampling profiler (core/prof/sampler.{h,cpp}).
//
// Validates the machinery WITHOUT OCCT: spin a known hot call chain on a worker thread,
// register that thread as the sample target, sample it, then assert the report captured
// multi-frame stacks. On Windows the test binary ships a PDB (CMake /Zi /DEBUG) so the hot
// function also shows up by name in the flat tables, proving symbolization + the stack walk
// line up. On Linux the hot functions live in an anonymous namespace (local symbols, absent
// from the dynamic symbol table dladdr reads), so the by-name check is Windows-only — the
// samples>0 + multi-frame (';') checks already prove the signal/backtrace path works.

#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <string>
#include <thread>

#include "prof/sampler.h"

namespace {
volatile double g_sink = 0.0;

// noinline so the call chain survives optimization and gives the stack walk something real
// to find (prof_hot_outer -> prof_hot_inner).
#if defined(_MSC_VER)
__declspec(noinline)
#elif defined(__GNUC__)
__attribute__((noinline))
#endif
double prof_hot_inner(int n) {
    double a = 0.0;
    for (int i = 1; i < n; ++i) a += std::sqrt(double(i)) * std::sin(double(i));
    return a;
}

#if defined(_MSC_VER)
__declspec(noinline)
#elif defined(__GNUC__)
__attribute__((noinline))
#endif
double prof_hot_outer(int reps) {
    double a = 0.0;
    for (int i = 0; i < reps; ++i) a += prof_hot_inner(997);
    return a;
}
}  // namespace

TEST_CASE("sampling profiler captures multi-frame stacks") {
#if !defined(_WIN32) && !defined(__linux__)
    MESSAGE("sampling profiler is unsupported on this platform; skipping");
#else
    using namespace aicad;
    std::atomic<bool> go{false};
    std::atomic<bool> done{false};

    std::thread worker([&]() {
        while (!go.load()) std::this_thread::yield();
        prof::set_target();  // register THIS (the busy) thread as the sample target
        auto t0 = std::chrono::steady_clock::now();
        while (std::chrono::steady_clock::now() - t0 < std::chrono::milliseconds(400)) {
            g_sink += prof_hot_outer(200);
        }
        prof::clear_target();
        done.store(true);
    });

    REQUIRE(prof::start(1000 /*us = 1 kHz*/, "all"));
    go.store(true);
    worker.join();
    prof::stop();

    nlohmann::json r = prof::report(nlohmann::json::object());
    INFO("report = " << r.dump(2));

    REQUIRE(r.value("ok", false));
    CHECK(r["summary"]["samples"].get<unsigned long long>() > 0);
    CHECK(r["modules"].size() > 0);
    // A ';' in the folded output means at least one captured stack had >1 frame — i.e. the
    // stack walk (RtlVirtualUnwind / backtrace) actually unwound past the leaf.
    CHECK(r["folded"].get<std::string>().find(';') != std::string::npos);

#if defined(_WIN32)
    // With the test binary's PDB present, the hot function should appear by name somewhere in
    // the self- or total-time tables (DbgHelp undecorates it to "prof_hot_inner/outer"). On
    // Linux these are anonymous-namespace (local) symbols, so dladdr can't name them and they
    // show as "test_prof!0x<rva>" — the checks above already prove the capture path.
    bool found = false;
    for (const char* key : {"flat_self", "flat_total"}) {
        for (const auto& f : r[key]) {
            if (f["frame"].get<std::string>().find("prof_hot") != std::string::npos) {
                found = true;
            }
        }
    }
    CHECK_MESSAGE(found, "expected prof_hot_* in the flat tables (needs the test PDB)");
#endif
#endif
}
