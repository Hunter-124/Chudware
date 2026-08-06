// Copilot streamed-delta row routing (GL-free, unit-tested by tests/test_stream_rows.cpp).
//
// A turn renders as ONE ordered column: thinking boxes, tool cards, context chips and the
// model's prose all appear at their arrival position. So the only question a streamed
// assistant_delta really asks is "which box do I join?", and getting it wrong is visible
// immediately — joining a box that other rows have already landed under puts new text ABOVE
// content the user has already read. That was the long-standing "the model thinks, says
// something, then the next thinking block jumps above the sentence" bug: the reverse scan
// reached back past intervening rows and kept growing the FIRST box of that channel.
//
// The rules live here, away from ImGui and AppState, because they are pure ordering logic
// and the failure mode is a silent visual one that no build error catches.

#pragma once

#include <string>
#include <vector>

namespace ui {

// One live-stream box in a turn's timeline, in arrival order.
struct StreamRowState {
    bool thinking  = false;   // reasoning box ("Thinking") vs. an output box
    bool ops       = false;   // output box classified as ops JSON ("Building ops…")
    bool streaming = false;   // still accepting deltas
    bool tail      = false;   // no other timeline row has landed after this one
};

// True when a delta payload is real content, and therefore a visible timeline boundary.
// Both delta coalescers (services/agent/agent_turn.py and core/agent/run.cpp) flush the
// active channel before switching, which can emit an empty or whitespace-only delta —
// treating that as a boundary splits ONE reasoning stream into several boxes.
bool streamTextVisible(const std::string& text);

// Index into `rows` of the box a delta on this channel continues, or -1 to open a new box at
// the tail. Never reaches back past a CLOSED box of the same channel: once a box is finalized
// its position in the column is fixed, and appending to it would render the new text above
// everything that landed since.
int streamDeltaTarget(const std::vector<StreamRowState>& rows, bool thinking);

// Index into `rows` of the live box on the OPPOSITE channel that this delta closes, or -1.
// Closing it is what makes the next burst on that channel open BELOW the text streaming now.
// An ops-JSON box is exempt: it is its own visible row and deliberately coalesces across
// stage bursts (see streamDeltaTarget's tail rule).
int streamChannelBoundary(const std::vector<StreamRowState>& rows, bool thinking);

}  // namespace ui
