// Phase 4 (shared engine host daemon): the ONE seam that decides where the
// GUI's C++-engine calls execute.
//
// Two backends behind one call() contract (the CppCore::call signature):
//   * DaemonConn  — aicad_ipc::EngineClient over the hostd unix socket
//                   (clientKind "gui"). DEFAULT since the Phase-8 flip
//                   (unset or AI_CAD_ENGINE_DAEMON=1).
//   * InProcCore  — legacy: core::cpp_core() dlopens libaicad into the GUI
//                   process. Selected ONCE at startup by an explicit
//                   AI_CAD_ENGINE_DAEMON=0 (kept >= one release; N+1 removes
//                   it). On daemon connect/spawn failure we log
//                   loudly and fall back to InProcCore (same pattern as
//                   services/mcp/core_bridge.py), so the GUI never hard-breaks.
//
// Threading: engine::call() runs on the async_rpc worker thread (EngineClient
// serializes internally). engine::client()->drainEvents() is called from the
// GUI thread each frame (host/src/core/engine_events.cpp) — EngineClient is
// internally locked and drainEvents try-locks so a long-running solve on the
// worker thread never stalls the render loop.
#pragma once

#include <string>
#include <functional>

#include <nlohmann/json.hpp>

namespace aicad_ipc { class EngineClient; }

namespace engine {

// True iff the daemon backend was selected AND connected at startup (resolved
// once, thread-safe). False = legacy in-process libaicad.
bool daemonMode();

// The live daemon connection (nullptr unless daemonMode()). GUI-thread event
// drain + daemon-only methods (attach_doc / edit_hold / subscribe) go here.
aicad_ipc::EngineClient* client();

// Route an engine call to the selected backend. Same contract as
// core::CppCore::call: true = rc==0 (result holds the payload); false = the
// method reported an error (structured {code,message} lands in `result`) or a
// transport failure (err only). Daemon extras handled here:
//   * {"code":"loading"} absorbed by polling daemon_status until ready.
//   * {"code":"read_only"} auto-upgrades the attach (attach_doc mode=rw) and
//     retries once — the "user edits a mirror tab" path, zero new UI.
// timeoutMs < 0 blocks forever; >= 0 bounds each daemon round trip (a timeout
// surfaces as a transport failure: false + err, no {code} in result). Ignored
// on the in-process backend — a call into the loaded DLL can't be timed out.
// `onEvent`, when set, streams {"event":"agent.event",...} pushes LIVE (mid-call) to the callback
// instead of buffering them for drainEvents() — used by the agent path so a blocking run's events
// surface as they happen on the daemon backend. No-op on the in-process backend (which streams via
// the cpp_core event sink set by the caller).
bool call(const std::string& method, const nlohmann::json& params,
          nlohmann::json& result, std::string& err, int timeoutMs = -1,
          const std::function<void(const nlohmann::json&)>& onEvent = {});

// Backend availability probes with InProcCore semantics (routing predicates in
// worker_client.cpp use these instead of core::cpp_core() directly). In daemon
// mode both report the connection state (the daemon owns the DLL).
bool available();
bool ensureLoaded(std::string& err);

// GUI-thread daemon liveness: probes the socket and, when dead, attempts a
// bounded reconnect with backoff (1s → 2s → 5s → every 10s). Connect-only —
// never spawns; the operator (or another client) must bring hostd back.
// InProc / non-daemon → Connected (no-op). Call once per frame from
// engineEventsTick before bootstrap/drain.
enum class DaemonConnState { InProc, Connected, Unreachable };
DaemonConnState maintainConnection();

}  // namespace engine
