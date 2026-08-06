// test_stream_rows.cpp — ctest for the copilot's streamed-delta row routing
// (ui::streamTextVisible / ui::streamDeltaTarget / ui::streamChannelBoundary). Links the REAL
// implementation (src/ui/copilot/stream_rows.cpp) — no GL context, no ImGui, no AppState.
//
// The regression under test, reported from a live session and missed by two earlier attempts:
// the model thinks, says a sentence, then thinks again — and the SECOND thinking block renders
// ABOVE the sentence. Two independent causes, both pinned here:
//
//  1. The delta scan reached backwards past intervening rows and kept growing the FIRST box of
//     that channel, so late reasoning was appended to a box that had already been drawn above
//     other content.
//  2. A channel switch was not a boundary at all, so one box per channel per turn collected
//     everything regardless of interleaving.
//
// The counter-pressure is real and must not regress either: both delta coalescers flush the
// active channel BEFORE switching, which can emit an empty or whitespace-only delta. Treating
// that as a boundary shatters one reasoning stream into several boxes (fixed 2026-08-05).
//
// Acceptance:
//   ✓ whitespace-only / empty deltas are not content
//   ✓ a non-ASCII byte IS content
//   ✓ a delta continues the live box of its own channel
//   ✓ a delta NEVER continues a closed box of its channel that later rows are stacked under
//   ✓ think -> prose -> think opens three boxes, in arrival order
//   ✓ prose closes the live thinking box and thinking closes the live prose box
//   ✓ an ops-JSON box survives a thinking burst and still coalesces at the tail
//   ✓ a buried closed ops box does NOT coalesce

#include "ui/copilot/stream_rows.h"

#include <cstdio>
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

using ui::StreamRowState;
using ui::streamChannelBoundary;
using ui::streamDeltaTarget;
using ui::streamTextVisible;

static StreamRowState row(bool thinking, bool streaming, bool tail, bool ops = false) {
    StreamRowState r;
    r.thinking  = thinking;
    r.ops       = ops;
    r.streaming = streaming;
    r.tail      = tail;
    return r;
}

// A tiny model of the turn column: rows in arrival order, with `tail` maintained for us.
struct Column {
    std::vector<StreamRowState> rows;

    void landOtherRow() {                       // a tool card / context chip / stage row
        for (auto& r : rows) r.tail = false;
    }
    // Apply one assistant_delta exactly the way feed.cpp does. Returns the row it landed in,
    // or -1 when the delta was dropped (a contentless channel flush with no box to join).
    int delta(bool thinking, const std::string& text) {
        const bool visible = streamTextVisible(text);
        if (visible) {
            const int close = streamChannelBoundary(rows, thinking);
            if (close >= 0) rows[(size_t)close].streaming = false;
        }
        int t = streamDeltaTarget(rows, thinking);
        if (t < 0) {
            if (!visible) return -1;    // never open a box for a contentless flush
            for (auto& r : rows) r.tail = false;
            rows.push_back(row(thinking, true, true));
            t = (int)rows.size() - 1;
        }
        rows[(size_t)t].streaming = true;
        return t;
    }
};

static void test_visibility() {
    CHECK(!streamTextVisible(""));
    CHECK(!streamTextVisible(" "));
    CHECK(!streamTextVisible("\n\n"));
    CHECK(!streamTextVisible(" \t\r\n"));
    CHECK(streamTextVisible("a"));
    CHECK(streamTextVisible("  x  "));
    CHECK(streamTextVisible("\xc2\xb7"));      // a middot is content
}

static void test_delta_continues_its_live_box() {
    std::vector<StreamRowState> rows{row(true, /*streaming=*/true, /*tail=*/true)};
    CHECK(streamDeltaTarget(rows, /*thinking=*/true) == 0);
    CHECK(streamDeltaTarget(rows, /*thinking=*/false) == -1);
}

static void test_delta_never_continues_a_buried_closed_box() {
    // thinking(closed) , prose(live)  — a thinking delta must NOT reach back to index 0.
    std::vector<StreamRowState> rows{
        row(true, /*streaming=*/false, /*tail=*/false),
        row(false, /*streaming=*/true, /*tail=*/true),
    };
    CHECK(streamDeltaTarget(rows, /*thinking=*/true) == -1);
    CHECK(streamDeltaTarget(rows, /*thinking=*/false) == 1);
}

// The headline case. Every box must land, and stay, at its arrival position.
static void test_think_prose_think_opens_three_boxes_in_order() {
    Column c;
    CHECK(c.delta(true,  "Let me measure the flange. ") == 0);
    CHECK(c.delta(true,  "It is 40mm.") == 0);            // same burst -> same box
    CHECK(c.delta(false, "The flange is 40mm across.") == 1);
    CHECK(c.delta(true,  "Now check the bolt circle.") == 2);
    CHECK(c.delta(false, "The bolt circle is 60mm.") == 3);
    CHECK(c.rows.size() == 4);
    CHECK(c.rows[0].thinking && !c.rows[1].thinking);
    CHECK(c.rows[2].thinking && !c.rows[3].thinking);
    // Only the newest box is still open — the three before it are pinned where they were drawn.
    CHECK(!c.rows[0].streaming && !c.rows[1].streaming && !c.rows[2].streaming);
    CHECK(c.rows[3].streaming);
}

// The counter-pressure: a coalescer flush must not shatter one reasoning stream.
static void test_empty_channel_flush_does_not_split_a_reasoning_stream() {
    Column c;
    CHECK(c.delta(true, "part one ") == 0);
    CHECK(c.delta(false, "") == -1);       // flush of the text channel: no content, no box
    CHECK(c.delta(false, "\n") == -1);     // and a whitespace-only one
    CHECK(c.delta(true, "part two") == 0);
    CHECK(c.rows.size() == 1);
}

// A visible row landing between two prose bursts (tool call, context chip, stage) closes the
// live boxes; the next burst must open below it, not grow the box above it.
static void test_prose_after_a_tool_card_opens_a_new_box() {
    Column c;
    CHECK(c.delta(false, "Building the bracket.") == 0);
    c.rows[0].streaming = false;          // feed.cpp closes the live boxes at a visible row
    c.landOtherRow();                     // ... and the tool card lands after them
    CHECK(c.delta(false, "Done — 3 features.") == 1);
    CHECK(c.rows.size() == 2);
}

static void test_channel_boundary_direction() {
    std::vector<StreamRowState> live{row(true, true, true)};
    CHECK(streamChannelBoundary(live, /*thinking=*/false) == 0);   // prose closes thinking
    CHECK(streamChannelBoundary(live, /*thinking=*/true) == -1);   // same channel: nothing
    std::vector<StreamRowState> prose{row(false, true, true)};
    CHECK(streamChannelBoundary(prose, /*thinking=*/true) == 0);   // thinking closes prose
    std::vector<StreamRowState> closed{row(false, false, true)};
    CHECK(streamChannelBoundary(closed, /*thinking=*/true) == -1);
}

// Ops JSON is its own visible row and deliberately coalesces across stage bursts, so a
// thinking burst must not close it — otherwise every stage stacks another "Building ops…".
static void test_ops_box_is_exempt_from_the_thinking_boundary() {
    std::vector<StreamRowState> rows{row(false, /*streaming=*/true, /*tail=*/true, /*ops=*/true)};
    CHECK(streamChannelBoundary(rows, /*thinking=*/true) == -1);
}

static void test_tail_ops_box_coalesces_but_a_buried_one_does_not() {
    std::vector<StreamRowState> tail{row(false, /*streaming=*/false, /*tail=*/true, /*ops=*/true)};
    CHECK(streamDeltaTarget(tail, /*thinking=*/false) == 0);

    std::vector<StreamRowState> buried{
        row(false, /*streaming=*/false, /*tail=*/false, /*ops=*/true),
        row(true, /*streaming=*/false, /*tail=*/true),
    };
    CHECK(streamDeltaTarget(buried, /*thinking=*/false) == -1);
}

int main() {
    test_visibility();
    test_delta_continues_its_live_box();
    test_delta_never_continues_a_buried_closed_box();
    test_think_prose_think_opens_three_boxes_in_order();
    test_empty_channel_flush_does_not_split_a_reasoning_stream();
    test_prose_after_a_tool_card_opens_a_new_box();
    test_channel_boundary_direction();
    test_ops_box_is_exempt_from_the_thinking_boundary();
    test_tail_ops_box_coalesces_but_a_buried_one_does_not();
    if (g_failures) {
        std::printf("test_stream_rows: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_stream_rows: all checks passed\n");
    return 0;
}
