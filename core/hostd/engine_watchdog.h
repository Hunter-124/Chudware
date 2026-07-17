// Engine-call watchdog policy (pure, header-only, dependency-free so it unit-tests trivially).
//
// The daemon runs OCCT engine calls per-client-thread; a doc is serialised by its own per-doc
// mutex (T-PERF5 DocLock), so a single pathological rebuild wedges only THAT doc's thread — the
// daemon keeps serving other docs/clients. But an OCCT call is non-interruptible in-process, so a
// genuinely-hung interactive rebuild can never be reclaimed without ending the process. The
// watchdog bounds exactly that: if a WATCHDOGGED call runs past the ceiling the daemon force-
// restarts (systemd Restart=on-failure) and docs recover from autosave (explicit, user-gated, so
// no auto-replay loop).
//
// Only fast INTERACTIVE modelling RPCs are watchdogged. Legitimately long-running RPCs
// (run_fea_*, run_cfd_*, agent_*, cam_*, export_*, import_*, doc_open, doc_save, reverse_*) run
// for minutes by design and MUST NOT trip it — they are deliberately excluded.
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

}  // namespace hostd
