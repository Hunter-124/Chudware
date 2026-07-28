// test_conv_ids.cpp — ctest for copilot session-id allocation (ui::convIdOrdinal /
// ui::nextFreeConvId). Links the REAL implementation (src/ui/copilot/conv_ids.cpp), not a
// copy of it — no GL context, no ImGui, no AppState needed.
//
// Regression under test (live, 2026-07-28): AppState::convCounter starts at 0 every launch
// and was never seeded from the store on disk, so a second run re-issued c1, c2, c3… on top
// of the transcripts already in <Documents>/Chudware/Sessions. The duplicate was then either
// dropped by persistChatHistory's writtenIds guard (the user's session vanished on reopen)
// or written over the older session's file — the observed corruption was an archived session
// titled "Knurled hex standoff" whose transcript was actually a different, later run.
//
// Acceptance:
//   ✓ convIdOrdinal parses "c<N>" and refuses everything else (no throw, no wrap)
//   ✓ a counter seeded from loaded ids never re-issues one of them
//   ✓ an id that only exists ON DISK (archived, not resident) is skipped too
//   ✓ repeated allocation is strictly increasing and never repeats
//   ✓ an empty store still starts at c1

#include "ui/copilot/conv_ids.h"

#include <algorithm>
#include <cassert>
#include <cstdio>
#include <set>
#include <string>
#include <vector>

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
        auto _a = (a);                                                           \
        auto _b = (b);                                                           \
        if (!(_a == _b)) {                                                       \
            std::printf("FAIL %s:%d: %s == %s\n", __FILE__, __LINE__, #a, #b);   \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

// Disk probe: the ids the fixture pretends already have an <id>.json on disk.
static std::set<std::string>* g_onDisk = nullptr;
static bool diskProbe(const std::string& id, void*) {
    return g_onDisk && g_onDisk->count(id) > 0;
}

// The rule conversations.cpp applies to every id it takes in from disk.
static int seedCounter(const std::vector<std::string>& loaded) {
    int counter = 0;
    for (const auto& id : loaded) counter = std::max(counter, ui::convIdOrdinal(id));
    return counter;
}

static void test_ordinal_parsing() {
    CHECK_EQ(ui::convIdOrdinal("c1"), 1);
    CHECK_EQ(ui::convIdOrdinal("c12"), 12);
    CHECK_EQ(ui::convIdOrdinal("c007"), 7);
    // Not the "c<N>" shape — must be 0, never a throw and never a wrapped value.
    CHECK_EQ(ui::convIdOrdinal(""), 0);
    CHECK_EQ(ui::convIdOrdinal("c"), 0);
    CHECK_EQ(ui::convIdOrdinal("c0"), 0);
    CHECK_EQ(ui::convIdOrdinal("cx"), 0);
    CHECK_EQ(ui::convIdOrdinal("c-1"), 0);
    CHECK_EQ(ui::convIdOrdinal("c1x"), 0);
    CHECK_EQ(ui::convIdOrdinal("1"), 0);
    CHECK_EQ(ui::convIdOrdinal("session-1"), 0);
    // std::stoi would throw out_of_range here; the parser must simply decline.
    CHECK_EQ(ui::convIdOrdinal("c99999999999999999999"), 0);
}

static void test_never_reissues_a_loaded_id() {
    // Exactly the user's store: c3, c4, c6, c7, c8, c10 already on disk and resident.
    const std::vector<std::string> loaded = {"c7", "c3", "c4", "c10", "c8", "c6"};
    std::set<std::string> disk(loaded.begin(), loaded.end());
    g_onDisk = &disk;

    int counter = seedCounter(loaded);
    CHECK_EQ(counter, 10);  // seeded past the HIGHEST id, not the count

    const std::string first = ui::nextFreeConvId(counter, loaded, diskProbe, nullptr);
    CHECK_EQ(first, std::string("c11"));
    CHECK(std::find(loaded.begin(), loaded.end(), first) == loaded.end());

    // The regression itself, stated as an executable fact: the OLD allocator was a bare
    // ++counter on a counter that started at 0 every launch and consulted neither the
    // resident set nor the disk. Replay it and watch it walk straight onto c3.
    int oldCounter = 0;
    std::vector<std::string> oldIssued;
    for (int i = 0; i < 4; ++i) oldIssued.push_back("c" + std::to_string(++oldCounter));
    CHECK_EQ(oldIssued.back(), std::string("c4"));
    const int collisions = (int)std::count_if(
        oldIssued.begin(), oldIssued.end(), [&](const std::string& id) {
            return std::find(loaded.begin(), loaded.end(), id) != loaded.end();
        });
    CHECK(collisions > 0);  // c3 and c4 — two transcripts at risk within four new sessions

    // Same four allocations through the shipped allocator: zero collisions.
    int newCounter = seedCounter(loaded);
    std::vector<std::string> taken = loaded;
    for (int i = 0; i < 4; ++i) {
        const std::string id = ui::nextFreeConvId(newCounter, taken, diskProbe, nullptr);
        CHECK(std::find(loaded.begin(), loaded.end(), id) == loaded.end());
        CHECK(disk.count(id) == 0);
        taken.push_back(id);
    }
    g_onDisk = nullptr;
}

static void test_skips_an_id_that_only_exists_on_disk() {
    // c2 is archived: NOT resident, but its transcript still owns Sessions/c2.json.
    const std::vector<std::string> resident = {"c1"};
    std::set<std::string> disk = {"c1", "c2", "c3"};
    g_onDisk = &disk;

    int counter = seedCounter(resident);  // only sees c1 -> 1
    const std::string id = ui::nextFreeConvId(counter, resident, diskProbe, nullptr);
    // Must clear c2 and c3 on the disk probe alone, or the next session overwrites one.
    CHECK_EQ(id, std::string("c4"));
    g_onDisk = nullptr;
}

static void test_repeated_allocation_is_unique_and_increasing() {
    const std::vector<std::string> resident = {"c2", "c5"};
    std::set<std::string> disk = {"c2", "c5", "c6"};
    g_onDisk = &disk;

    int counter = seedCounter(resident);
    std::set<std::string> handed;
    std::vector<std::string> order;   // set<string> sorts lexicographically — keep issue order
    std::vector<std::string> taken = resident;
    int prev = 0;
    for (int i = 0; i < 8; ++i) {
        const std::string id = ui::nextFreeConvId(counter, taken, diskProbe, nullptr);
        CHECK(handed.insert(id).second);                 // never repeats
        CHECK(ui::convIdOrdinal(id) > prev);             // strictly increasing
        CHECK(disk.count(id) == 0);                      // never lands on a transcript
        prev = ui::convIdOrdinal(id);
        order.push_back(id);
        taken.push_back(id);                             // it is resident from now on
    }
    // Seeded to 5 by c5, so c6 is skipped on the disk probe and the run starts at c7.
    CHECK_EQ(order.front(), std::string("c7"));
    CHECK_EQ(order.back(), std::string("c14"));
    g_onDisk = nullptr;
}

static void test_empty_store_starts_at_c1() {
    std::set<std::string> disk;
    g_onDisk = &disk;
    int counter = seedCounter({});
    CHECK_EQ(ui::nextFreeConvId(counter, {}, diskProbe, nullptr), std::string("c1"));
    g_onDisk = nullptr;
}

int main() {
    test_ordinal_parsing();
    test_never_reissues_a_loaded_id();
    test_skips_an_id_that_only_exists_on_disk();
    test_repeated_allocation_is_unique_and_increasing();
    test_empty_store_starts_at_c1();
    if (g_failures) {
        std::printf("test_conv_ids: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_conv_ids: all checks passed\n");
    return 0;
}
