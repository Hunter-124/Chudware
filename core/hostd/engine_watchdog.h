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
//   * FAST (engineMethodWatchdogged): the interactive modelling RPCs a healthy engine always
//     answers in well under a second. Ceiling ~90 s, CHUDWARE_HOSTD_ENGINE_WATCHDOG_S.
//   * LONG (everything else — run_fea_*, run_cfd_*, agent_*, cam_*, export_*, import_*,
//     doc_open, doc_save, reverse_*): legitimately minutes-long by design, so the fast ceiling
//     must never touch them. They are NOT exempt from every ceiling though — an unbounded call
//     holds its dispatch thread forever, Server::Status::inflight never returns to 0, and
//     idleExpired() can therefore never fire again: the daemon becomes unkillable by its own
//     self-healing. Hence a deliberately huge second ceiling
//     (CHUDWARE_HOSTD_ENGINE_WATCHDOG_LONG_S) whose only job is to catch a call that will
//     NEVER return.
// The tiers are disjoint: a method is judged by exactly one ceiling, never both.
#pragma once

#include <string_view>

namespace hostd {

// True for the interactive modelling RPCs a healthy engine always answers in well under a second.
inline bool engineMethodWatchdogged(std::string_view m) {
    return m == "doc_apply_ops" || m == "doc_rebuild" || m == "doc_undo" ||
           m == "doc_redo" || m == "doc_activate" || m == "doc_regen" ||
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
