// test_switch_hold.cpp — ctest for core::SwitchHold (host/src/core/switch_hold.h), the state
// AppState::ensureActiveTab consults before re-pointing the tab strip at the mirror's doc.
//
// Regression under test (live, 2026-09-27): the user on "Part1" clicked the AI mirror tab while
// an agent turn ran. switchToTab selected the AI tab and queued its doc_activate; one frame later
// the per-frame reconcile saw the mirror still holding Part1 and snapped activeTab back — "it
// flashes the loading icon for a frame, kicks me back to the last part and deselects the tab".
// The GUI log showed the same pattern for every click (gen=1..5, none adopted).
//
// Acceptance:
//   ✓ a pending activation keeps the clicked tab selected on every frame until it lands
//   ✓ its landing (mirror adopted) or failure releases the hold; a failure lets the reconcile
//     return the strip to the doc the mirror still holds
//   ✓ a newer switch owns the hold; the older activation finishing late does not release it
//   ✓ a doc-creating path that moves the mirror elsewhere is still followed
//   ✓ a tab the user left behind is not held

#include "core/switch_hold.h"

#include <cstdio>

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

using core::SwitchHold;

static void pendingActivationKeepsTheClickedTab() {
    SwitchHold h;
    h.begin("ai", "part1");
    for (int frame = 0; frame < 600; ++frame)  // ten seconds of frames while it's queued
        CHECK(h.keepsActiveTab("ai", "part1"));
    h.end("ai");                               // adopted: the mirror now holds "ai"
    CHECK(!h.keepsActiveTab("ai", "part1"));
    CHECK(!h.keepsActiveTab("ai", "ai"));
}

static void mirrorCatchingUpNeedsNoHold() {
    SwitchHold h;
    h.begin("ai", "part1");
    // A pushed snapshot bound the mirror before the activation answered.
    CHECK(!h.keepsActiveTab("ai", "ai"));
}

static void failureReturnsTheStripToTheMirror() {
    SwitchHold h;
    h.begin("ai", "part1");
    h.end("ai");  // doc_activate failed: toast, and the reconcile goes back to part1
    CHECK(!h.keepsActiveTab("ai", "part1"));
}

static void newestSwitchOwnsTheHold() {
    SwitchHold h;
    h.begin("ai", "part1");
    h.begin("other", "part1");                // clicked a second cold tab (coalesced)
    CHECK(h.keepsActiveTab("other", "part1"));
    CHECK(!h.keepsActiveTab("ai", "part1"));  // not selected any more
    h.end("ai");                              // the superseded first activation lands late
    CHECK(h.keepsActiveTab("other", "part1"));
    h.end("other");
    CHECK(!h.keepsActiveTab("other", "part1"));
}

static void mirrorMovedByADocCreatingPathIsFollowed() {
    SwitchHold h;
    h.begin("ai", "part1");
    // New Part / Open re-pointed the mirror to a third document: the reconcile adopts it.
    CHECK(!h.keepsActiveTab("ai", "new-part"));
}

static void userLeftTheTab() {
    SwitchHold h;
    h.begin("ai", "part1");
    // Clicked back to the warm Part1 (synchronous bind): nothing to hold there.
    CHECK(!h.keepsActiveTab("part1", "part1"));
    // A same-doc repeat click re-arms from the doc the mirror holds now.
    h.begin("ai", "part1");
    CHECK(h.keepsActiveTab("ai", "part1"));
}

int main() {
    pendingActivationKeepsTheClickedTab();
    mirrorCatchingUpNeedsNoHold();
    failureReturnsTheStripToTheMirror();
    newestSwitchOwnsTheHold();
    mirrorMovedByADocCreatingPathIsFollowed();
    userLeftTheTab();
    if (g_failures) {
        std::printf("test_switch_hold: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_switch_hold: all passed\n");
    return 0;
}
