// Engine-call watchdog policy (pure, header-only, dependency-free so it unit-tests trivially).
//
// The daemon runs OCCT engine calls per-client-thread; a doc is serialised by its own per-doc
// mutex (T-PERF5 DocLock), so a single pathological rebuild wedges only THAT doc's thread — the
// daemon keeps serving other docs/clients. But an OCCT call is non-interruptible in-process, so
// a genuinely-hung call can never be reclaimed without ending the process. The watchdog bounds
// exactly that: if a call runs past its ceiling the daemon force-restarts (systemd
// Restart=on-failure) and docs recover from autosave (explicit, user-gated, so no auto-replay
// loop).
//
// TWO TIERS, because "overdue" means wildly different things per RPC. Every call the daemon
// tracks (Server::beginInflight — engine passthrough only) falls in exactly one of them:
//   * FAST (engineMethodWatchdogged): bounded, non-modeling calls such as ping.
//     Ceiling ~90 s, CHUDWARE_HOSTD_ENGINE_WATCHDOG_S.
//   * LONG (everything else, including modeling/rebuild mutations): work scales with
//     document/input size and may legitimately take minutes. This also covers shadow
//     preflight doc_apply_ops and cold doc_activate rebuilds without caller hints.
//     CHUDWARE_HOSTD_ENGINE_WATCHDOG_LONG_S bounds these calls so a genuine hang cannot
//     hold inflight above zero and prevent idle exit forever.
// The tiers are disjoint: a method is judged by exactly one ceiling, never both.
#pragma once

#include <string_view>

namespace hostd {

// Only calls that cannot rebuild geometry or wait on a document's modeling lock belong here.
inline bool engineMethodWatchdogged(std::string_view m) {
    return m == "ping" ||
           m == "__hostd_test_stall";  // test-only probe (server.cpp), never a real client RPC
}

// True when a watchdogged call has run past the ceiling. ceilingMs <= 0 disables the watchdog.
inline bool engineCallOverdue(std::string_view method, long long elapsedMs, long long ceilingMs) {
    return ceilingMs > 0 && engineMethodWatchdogged(method) && elapsedMs > ceilingMs;
}

// True when a LONG-tier (non-interactive) call has blown the generous ceiling. Kept separate
// from engineCallOverdue rather than folded into it so the two tiers can never be confused: an
// interactive RPC is judged ONLY by the fast ceiling and a long-running one ONLY by this, which
// is exactly why the fast tier's semantics are unchanged. ceilingMs <= 0 disables this tier.
inline bool engineCallOverdueLong(std::string_view method, long long elapsedMs,
                                  long long ceilingMs) {
    return ceilingMs > 0 && !engineMethodWatchdogged(method) && elapsedMs > ceilingMs;
}

}  // namespace hostd
