// test_tess_catchup.cpp — ctest for core::TessCatchup (host/src/core/tess_catchup.h), the
// per-document bookkeeping engine_events.cpp uses to bring the active viewport's mesh up to
// date after a revision that carried no tessellation.
//
// Regression under test (live, 2026-09-27): asm_mate / asm_add_component / pose sweeps publish
// with_tess:false, so the active assembly kept drawing the previous mesh for a whole
// mate+sweep stretch (revisions 131–165 of the user's open assembly, ~10 min) until an
// unrelated mesh-bearing revision arrived at the end of the turn.
//
// Acceptance:
//   ✓ the first tess-less revision asks for a fetch; later ones while it is in flight don't
//   ✓ an answer matching the displayed tree is drawn
//   ✓ an answer older than a revision that landed meanwhile refetches (never draws stale)
//   ✓ a mesh-bearing revision, leaving the tab, or a failed fetch drops the want
//   ✓ an answer ahead of the displayed tree is not drawn (its own push takes over)
//   ✓ documents are independent

#include "core/tess_catchup.h"

#include <cstdio>

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

using core::TessCatchup;
using Outcome = TessCatchup::Outcome;

static void coalescesAndCommits() {
    TessCatchup c;
    CHECK(c.wantMesh("asm", 131));
    CHECK(c.inFlight("asm"));
    CHECK(!c.wantMesh("asm", 131));  // same revision again: still one request
    CHECK(c.onFetched("asm", true, 131, true, 131) == Outcome::Commit);
    CHECK(!c.inFlight("asm"));
    CHECK(c.wantMesh("asm", 132));   // the next tess-less revision asks again
}

static void staleAnswerRefetchesOnce() {
    TessCatchup c;
    CHECK(c.wantMesh("asm", 140));
    CHECK(!c.wantMesh("asm", 141));  // sweep keeps posing while the fetch runs
    CHECK(!c.wantMesh("asm", 142));
    // The daemon answered at 140; the viewport tree is already at 142.
    CHECK(c.onFetched("asm", true, 140, true, 142) == Outcome::Refetch);
    CHECK(c.inFlight("asm"));        // the caller issues exactly one follow-up
    CHECK(!c.wantMesh("asm", 142));
    CHECK(c.onFetched("asm", true, 142, true, 142) == Outcome::Commit);
    CHECK(!c.inFlight("asm"));
}

static void meshBearingRevisionSatisfies() {
    TessCatchup c;
    CHECK(c.wantMesh("asm", 150));
    c.satisfied("asm");              // add_features on the assembly pushed its own mesh
    CHECK(c.onFetched("asm", true, 150, true, 151) == Outcome::Drop);
    CHECK(!c.inFlight("asm"));
    c.satisfied("never-seen");       // harmless
    CHECK(c.wantMesh("asm", 152));
}

static void leavingOrFailingDrops() {
    TessCatchup c;
    CHECK(c.wantMesh("asm", 160));
    CHECK(!c.wantMesh("asm", 161));
    CHECK(c.onFetched("asm", true, 160, /*viewing=*/false, 161) == Outcome::Drop);
    CHECK(!c.inFlight("asm"));

    CHECK(c.wantMesh("asm", 162));
    CHECK(c.onFetched("asm", /*usable=*/false, -1, true, 162) == Outcome::Drop);
    CHECK(!c.inFlight("asm"));       // no retry loop against a failing daemon
    CHECK(c.wantMesh("asm", 163));
}

static void answerAheadOfTreeIsNotDrawn() {
    TessCatchup c;
    CHECK(c.wantMesh("asm", 170));
    // Engine is already at 171 but that push is still queued behind this callback.
    CHECK(c.onFetched("asm", true, 171, true, 170) == Outcome::Drop);
    CHECK(!c.inFlight("asm"));
}

static void documentsAreIndependent() {
    TessCatchup c;
    CHECK(c.wantMesh("a", 1));
    CHECK(c.wantMesh("b", 7));
    CHECK(c.onFetched("b", true, 7, true, 7) == Outcome::Commit);
    CHECK(c.inFlight("a"));
    CHECK(c.onFetched("unknown", true, 1, true, 1) == Outcome::Drop);
}

int main() {
    coalescesAndCommits();
    staleAnswerRefetchesOnce();
    meshBearingRevisionSatisfies();
    leavingOrFailingDrops();
    answerAheadOfTreeIsNotDrawn();
    documentsAreIndependent();
    if (g_failures) {
        std::printf("test_tess_catchup: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_tess_catchup: all checks passed\n");
    return 0;
}
