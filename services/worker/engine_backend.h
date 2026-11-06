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
//                   A `daemon_draining` hello refusal never falls back: daemon
//                   mode stays on and maintainConnection() waits for the
//                   draining hostd to exit (see daemonRefusal()).
//
// Threading: GUI document calls share their primary daemon connection and its
// registry identity. Allowlisted document-free queries from background threads
// use independent EngineClient sockets; slow catalog/auth discovery cannot hold
// the GUI's document socket. drainEvents() try-locks every socket it polls and
// preserves the primary event stream. Long agent/simulation runs keep their lanes.
#pragma once

#include <string>
#include <functional>
#ifndef __EMSCRIPTEN__
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>
#endif

#include <nlohmann/json.hpp>

namespace chudware_ipc { class EngineClient; }

namespace engine {
// Browser protocol admission is shared by dispatch and UI capability disclosure.
// The advertised list is authoritative; unknown/older gateways never enable CAD/AI.
inline bool webCellProtocolReady(const nlohmann::json& info) {
    if (!info.is_object()) return false;
    const auto version = info.find("web_rpc_version");
    const auto methods = info.find("web_rpc_methods");
    if (version == info.end() || !version->is_number_integer() || *version != 2 ||
        methods == info.end() || !methods->is_array()) return false;
    for (const auto& entry : *methods)
        if (!entry.is_string()) return false;
    return true;
}

inline bool webCellMethodAllowed(const nlohmann::json& info, const std::string& method) {
    if (method == "hello" || method == "ping") return true;
    if (!webCellProtocolReady(info)) return false;
    for (const auto& entry : info["web_rpc_methods"])
        if (entry.get_ref<const std::string&>() == method) return true;
    return false;
}


enum class DaemonConnState { InProc, Connected, Unreachable };
#ifndef __EMSCRIPTEN__
// Native GUI lifetime: connect/spawn/hello and proactive reconnect run on one worker.
// On this thread daemonMode() reports the requested backend while initialization is
// pending; client() is null until the final daemon-vs-inproc verdict is published.
// Synchronous GUI dispatch rejects pending initialization with engine_starting.
// Shutdown cancels retries/connect/hello/version/drain waits, aborts the active
// transport, then joins; it never terminates the shared daemon.
// Other threads retain the blocking startup contract (notably WorkerClient's engine pin).
class GuiConnection {
public:
    explicit GuiConnection(void (*wake)());
    ~GuiConnection();
    void shutdown();
    GuiConnection(const GuiConnection&) = delete;
    GuiConnection& operator=(const GuiConnection&) = delete;
private:
    friend DaemonConnState maintainConnection();
    friend chudware_ipc::EngineClient* client();
    void requestMaintenance();
    std::thread worker_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::atomic<bool> busy_{true};
    bool requested_ = false, quit_ = false;
};
// Nonblocking; the worker's final backend verdict is required before spawning a
// Python worker, so both processes get the same CHUDWARE_ENGINE_DAEMON pin.
bool connectionPending();
#endif
// True iff the daemon backend was selected at startup (resolved once, thread-safe),
// including a draining daemon that refuses this client. A pending GuiConnection
// reports the requested backend; false after initialization means legacy in-process.
bool daemonMode();

// True only when daemon mode was WANTED (no explicit CHUDWARE_ENGINE_DAEMON=0)
// but every connect attempt failed and we fell back to the in-process core.
// Distinguishes "legacy on purpose" from "legacy because the daemon was
// unreachable" — the latter means this process cannot see documents owned by a
// shared hostd, which is a user-visible defect worth surfacing.
bool daemonUnavailable();

// The `daemon_draining` hello error from the shared connection's last connect attempt
// (Server::drainRefusal: reason, daemon_pid, active_clients, message, hint, ...), or
// null. An independent GUI connection is refused during a drain; the GUI shows
// this on its unreachable chip. Safe to call from any thread.
nlohmann::json daemonRefusal();

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

// Teardown-only: wake blocked agent and simulation runs on their separate dedicated
// connections so app quit / AsyncRpc shutdown never hangs. No-op for unopened connections.
void abortRunClient();

// The dedicated daemon connections a method streams on. Agent turns (agent_admin_turn)
// lease one of at most eight private connections, waiting when all are occupied. They fall
// back to the shared GUI connection only when none can open and the pool is empty.
// Simulation solves and sim_mesh use one private connection with no fallback. The lanes
// are independent: a chat turn never waits for a solve.
enum class RunLane { None, Agent, Sim };
RunLane runLane(const std::string& method);

// A simulation-lane call that is not a solve (sim_mesh, the mesh preview). It may borrow the
// simulation connection while it is idle, but must never queue behind a solve holding it:
// WorkerClient sends it over a short-lived side connection while the lane is busy.
bool isSimLanePreview(const std::string& method);

// True when the simulation lane's dedicated connection exists or can be opened lazily.
// WorkerClient uses this to release a solve from callMtx_ onto the simulation lane mutex.
// Online daemon agent turns never take either mutex. False off daemon mode.
bool simRunConnectionReady();

// Backend availability probes with InProcCore semantics (routing predicates in
// worker_client.cpp use these instead of core::cpp_core() directly). In daemon
// mode both report the connection state (the daemon owns the DLL).
bool available();
bool ensureLoaded(std::string& err);

// GUI-thread daemon liveness: probes the socket and, when dead, queues a reconnect
// on GuiConnection's worker with backoff (1s → 2s → 5s → every 10s). Without a
// GuiConnection, callers retain the blocking bounded-reconnect contract. After 3 failed
// connects it spawns a new hostd, rate limited — never while a draining hostd
// refuses this client (daemonRefusal()), because that daemon still holds the
// instance lock. InProc / non-daemon → InProc (no-op). Call once per frame from
// engineEventsTick before bootstrap/drain.
DaemonConnState maintainConnection();

}  // namespace engine
