// Session-id allocation for copilot conversations — deliberately GL/ImGui/AppState-free so
// host/tests/test_conv_ids.cpp can link the REAL implementation rather than a copy of it.
//
// Why this exists: AppState::convCounter is a plain int that starts at 0 on EVERY launch and
// was never seeded from the store on disk. A second run therefore handed out c1, c2, c3…
// straight on top of the c3/c7/c8 transcripts already in <Documents>/Chudware/Sessions, and
// the collision was resolved by silently dropping one of the two (persistChatHistory's
// writtenIds guard) or by one overwriting the other's file. Both are data loss: the user ran
// a session, closed the app, and it was gone on reopen.
#pragma once

#include <string>
#include <vector>

namespace ui {

// The numeric suffix of a "c<N>" session id, or 0 when *id* is not that shape.
// "c12" -> 12, "c0" -> 0, "cx" -> 0, "" -> 0, "c-1" -> 0.
int convIdOrdinal(const std::string& id);

// The next free session id, given the ids already resident in memory and a predicate that
// answers "does a transcript for this id already exist on disk?".
//
// *counter* is advanced past every id handed out, so repeated calls never repeat. The disk
// probe matters independently of *taken*: an archived session is not always resident, yet it
// still owns <id>.json and must not be clobbered.
std::string nextFreeConvId(int& counter,
                           const std::vector<std::string>& taken,
                           bool (*existsOnDisk)(const std::string& id, void* ctx),
                           void* ctx);

}  // namespace ui
