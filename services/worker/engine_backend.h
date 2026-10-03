// Phase 4 (shared engine host daemon): the ONE seam that decides where the
// GUI's C++-engine calls execute.
//
// Two backends behind one call() contract (the CppCore::call signature):
//   * DaemonConn  — chudware_ipc::EngineClient over the hostd unix socket
//                   (clientKind "gui"). DEFAULT since the Phase-8 flip
//                   (unset or CHUDWARE_ENGINE_DAEMON=1).
//   * InProcCore  — legacy: core::cpp_core() dlopens libchudware into the GUI
//                   process. Selected ONCE at startup by an explicit
//                   CHUDWARE_ENGINE_DAEMON=0 (kept >= one release; N+1 removes
//                   it). On daemon connect/spawn failure we retry with bounded
//                   backoff (~2.85 s total; CHUDWARE_ENGINE_DAEMON_RETRY_MS
//                   overrides that budget in milliseconds, 0 = single attempt)
//                   and only then log loudly and fall back to InProcCore (same
//                   pattern as services/mcp/core_bridge.py), so the GUI never
//                   hard-breaks. The retry exists because this decision is
//                   latched for the whole process: a hostd that was merely
//                   restarting at launch used to cost the entire session.
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

namespace chudware_ipc { class EngineClient; }

namespace engine {

// True iff the daemon backend was selected AND connected at startup (resolved
// once, thread-safe). False = legacy in-process libchudware.
bool daemonMode();

// True only when daemon mode was WANTED (no explicit CHUDWARE_ENGINE_DAEMON=0)
// but every connect attempt failed and we fell back to the in-process core.
// Distinguishes "legacy on purpose" from "legacy because the daemon was
// unreachable" — the latter means this process cannot see documents owned by a
// shared hostd, which is a user-visible defect worth surfacing.
bool daemonUnavailable();

// The live daemon connection (nullptr unless daemonMode()). GUI-thread event
// drain + daemon-only methods (attach_doc / edit_hold / subscribe) go here.
chudware_ipc::EngineClient* client();

// hostd binary hint used for daemon discovery (exe-adjacent, then the repo
// dev tree). Short-lived side-channel EngineClients MUST set
// Options.hostdBin = hostdBinHint() — an empty hint makes instanceKey() hash
// "default", which is a DIFFERENT socket than the running daemon's
// (instanceKey(selfPath())), so the side connect fails ENOENT mid-session.
std::string hostdBinHint();

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
// instead of buffering them for drainEvents() — used by agent and topology runs so their events
// surface as they happen on the daemon backend. No-op on the in-process backend (which streams via
// the cpp_core event sink set by the caller).
bool call(const std::string& method, const nlohmann::json& params,
          nlohmann::json& result, std::string& err, int timeoutMs = -1,
          const std::function<void(const nlohmann::json&)>& onEvent = {});

// Teardown-only: wake blocked agent/mission and simulation runs on their separate dedicated
// connections so app quit / AsyncRpc shutdown never hangs. No-op for unopened connections.
void abortRunClient();

// The dedicated daemon connection a method streams on. Agent runs (agent_admin_turn,
// agent_run[_v2], agent_mission[_v1]) take the agent run connection, which falls back to the
// shared GUI connection when it cannot open; simulation solves and sim_mesh take the private
// simulation connection, which never falls back (the call fails instead). Everything else uses
// the shared GUI connection. The two lanes are independent: a chat turn never waits for a solve.
enum class RunLane { None, Agent, Sim };
RunLane runLane(const std::string& method);

// A simulation-lane call that is not a solve (sim_mesh, the mesh preview). It may borrow the
// simulation connection while it is idle, but must never queue behind a solve holding it:
// WorkerClient sends it over a short-lived side connection while the lane is busy.
bool isSimLanePreview(const std::string& method);

// True when daemon-mode calls on `lane` will stream on that lane's dedicated connection
// (opening it lazily if needed), i.e. engine::call will NOT occupy the shared GUI connection.
// WorkerClient only lets a long run off its call mutex when this holds for the run's own lane;
// one lane's readiness says nothing about the other's. False for RunLane::None and off daemon
// mode.
bool runConnectionReady(RunLane lane);

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
