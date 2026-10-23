// Engine-call watchdog policy (pure, header-only, dependency-free so it unit-tests trivially).
//
// The daemon runs OCCT engine calls per-client-thread; a doc is serialised by its own per-doc
// mutex (DocLock), so a single pathological rebuild wedges only THAT doc's thread — the
// daemon keeps serving other docs/clients. But an OCCT call is non-interruptible in-process, so
// a genuinely-hung call can never be reclaimed without ending the process. The watchdog bounds
// exactly that: if a call runs past its ceiling the daemon force-restarts (systemd
// Restart=on-failure) and docs recover from autosave (explicit, user-gated, so no auto-replay
// loop).
//
// Two disjoint engine-call tiers. Agent/chudcode calls have their own transport
// silence/absolute limits, so elapsed wall time cannot diagnose a stuck turn.
//   * FAST: bounded non-modeling engine calls (~90 s).
//   * LONG: modeling/rebuild mutations (~60 min), including shadow preflight
//     doc_apply_ops and cold doc_activate rebuilds.
// An engine method is judged by at most one ceiling.
#pragma once

#include <string_view>

namespace hostd {

// Only calls that cannot rebuild geometry or wait on a document's modeling lock belong here.
inline bool engineMethodWatchdogged(std::string_view m) {
    return m == "ping" ||
           m == "__hostd_test_stall";  // test-only probe (server.cpp), never a real client RPC
}

inline bool engineMethodTurn(std::string_view m) {
    return m.substr(0, 6) == "agent_" || m.substr(0, 9) == "chudcode_";
}

// True when a watchdogged call has run past the ceiling. ceilingMs <= 0 disables the watchdog.
inline bool engineCallOverdue(std::string_view method, long long elapsedMs, long long ceilingMs) {
    return ceilingMs > 0 && engineMethodWatchdogged(method) && elapsedMs > ceilingMs;
}

// The generous wall-clock ceiling applies only to engine calls, not agent turns
// whose streamed progress can continue for hours.
inline bool engineCallOverdueLong(std::string_view method, long long elapsedMs,
                                  long long ceilingMs) {
    return ceilingMs > 0 && !engineMethodWatchdogged(method) &&
           !engineMethodTurn(method) && elapsedMs > ceilingMs;
}

}  // namespace hostd
