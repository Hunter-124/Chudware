// Regression coverage for per-document mesh obligations and transient failures.
#include "core/tess_catchup.h"

#include <chrono>
#include <cstdio>

static int g_failures = 0;
#define CHECK(cond) do { if (!(cond)) { \
    std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); ++g_failures; \
} } while (0)

using core::TessCatchup;
using namespace std::chrono_literals;

static void coalescesUntilMeshCommit() {
    TessCatchup c;
    const auto now = TessCatchup::Clock::now();
    c.require("asm", 131);
    CHECK(c.beginFetch("asm", now));
    c.require("asm", 132);
    CHECK(!c.beginFetch("asm", now));
    c.fetched("asm", true, now);
    CHECK(c.pending("asm"));
    CHECK(c.wanted("asm") == 132);
    c.loading("asm", 132);
    CHECK(!c.beginFetch("asm", now));
    c.finished("asm", 131, true, now); // older mesh cannot satisfy a newer tree
    CHECK(c.pending("asm"));
    c.finished("asm", 132, true, now);
    CHECK(!c.pending("asm"));
}

static void retryFailuresAndDeferredLoads() {
    TessCatchup c;
    const auto now = TessCatchup::Clock::now();
    c.require("part", 7);
    CHECK(c.beginFetch("part", now));
    c.fetched("part", false, now);
    CHECK(c.pending("part"));
    CHECK(!c.beginFetch("part", now + 100ms));
    CHECK(c.beginFetch("part", now + 250ms));
    c.fetched("part", true, now + 250ms);
    c.loading("part", 7);
    c.finished("part", 7, false, now + 250ms); // parse failure or edit-mode deferral
    CHECK(c.pending("part"));
    CHECK(!c.beginFetch("part", now + 500ms));
    CHECK(c.beginFetch("part", now + 750ms));
    c.fetched("part", true, now + 750ms);
    c.loading("part", 7);
    c.finished("part", 7, true, now + 750ms);
    CHECK(c.empty());
}

static void newerAndIndependentDocuments() {
    TessCatchup c;
    const auto now = TessCatchup::Clock::now();
    c.require("a", 1);
    c.require("b", 8);
    c.loading("a", 1);
    c.require("a", 2);
    c.finished("a", 1, false, now); // superseded loader cannot cancel the newer obligation
    CHECK(c.beginFetch("a", now));
    CHECK(c.beginFetch("b", now));
    c.fetched("b", true, now);
    c.loading("b", 8);
    c.finished("b", 8, true, now);
    CHECK(c.pending("a"));
    CHECK(!c.pending("b"));
    c.forget("a");
    CHECK(c.empty());
}

int main() {
    coalescesUntilMeshCommit();
    retryFailuresAndDeferredLoads();
    newerAndIndependentDocuments();
    if (g_failures) {
        std::printf("test_tess_catchup: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_tess_catchup: all checks passed\n");
    return 0;
}
