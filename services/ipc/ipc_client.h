// EngineClient — a client connection to the shared engine-host daemon.
//
// Drop-in sibling of core::CppCore (services/worker/cpp_core.h): same blocking
// call() contract, so the GUI's routing seam (worker_client.cpp) and tests can
// swap backends without signature churn. Adds connect-or-spawn discovery and a
// queue of daemon push events (doc.changed etc., drained by the GUI per frame).
//
// Threading: hostd processes each connection sequentially. GUI document calls
// keep the primary connection's attachment/hold/subscription identity; explicitly
// allowed document-free background queries use a lazily opened connection per
// calling thread, so cold worker discovery never holds the GUI socket mutex.
// Events on the primary connection are queued for drainEvents(); private query
// connections discard duplicate broadcasts but keep live call() callbacks.
#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <thread>

#include <nlohmann/json.hpp>

#include "ipc_transport.h"

namespace chudware_ipc {

class EngineClient {
public:
    struct Options {
        std::string hostdBin;   // spawn candidate; "" = env/discovery only
        std::string clientKind = "test";  // "gui" | "mcp" | "agent" | "test"
        // Optional multi-user identity (web bridge / multi-seat). Sent in hello
        // as params.user = {id, display} when userId is non-empty.
        std::string userId;
        std::string userDisplay;
        int spawnBudgetMs = 15000;        // total connect-or-spawn budget
        bool allowSpawn = true;           // false = attach-only (never spawn)
        // Private lanes nobody drains (engine_backend's simulation connection): drop every
        // push the caller does not consume live instead of queueing it for drainEvents().
        // agent.event payloads still reach a call()'s onEvent; everything else (doc.*,
        // broadcasts, agent.event without onEvent) is read and discarded on every receive
        // path, so the queue stays empty however long a call streams. Default false keeps
        // ordinary GUI/agent buffering unchanged.
        bool discardUnconsumedEvents = false;
    };

    // Connect to the daemon for this instance key, spawning one if absent (and
    // allowed). Performs the hello handshake. False + err on failure. A daemon that
    // refuses hello (see helloError()) is not a missing daemon: nothing is spawned.
    bool connectOrSpawn(const Options& opts, std::string& err);

    bool connected() const { return connected_.load(std::memory_order_acquire); }
    void disconnect();
    // Teardown abort: wake every in-flight primary/background read without
    // waiting for its round trip. The next call may reconnect. Safe from any thread.
    void abortPending();

    // Shared with WorkerClient: these GUI background queries need neither its
    // stdio-pipe call mutex nor the primary document connection. Unknown methods,
    // any doc_id parameter, and calls on the primary thread always return false.
    bool usesBackgroundConnection(const std::string& method,
                                  const nlohmann::json& params) const;

    // The hello response: {proto, abi, engine_path, git_rev, daemon_pid, state, id_remap}.
    const nlohmann::json& serverInfo() const { return serverInfo_; }

    // The error object the daemon answered the last hello with, for example the
    // `daemon_draining` refusal of a draining hostd (Server::drainRefusal):
    // {code, reason, daemon_pid, clients, active_clients, children, hard_stop,
    // changed_path?, message, hint}. Null when the last connect attempt succeeded or
    // failed without a hello answer. Each connect attempt (connectOrSpawn, tryReconnect,
    // the reconnect inside call()) replaces it when the attempt ends, so a reader never
    // sees a refusal disappear while the next attempt is in flight. Safe from any thread.
    nlohmann::json helloError() const;

    // Blocking JSON-RPC round trip; same contract as CppCore::call. Returns true
    // iff the daemon answered with "result"; err carries transport or RPC errors
    // ("error" payloads are also copied into result for callers that inspect them).
    // An id:null JSON-RPC protocol error also surfaces its error payload and retires
    // the socket: it cannot be correlated safely with any later request. Malformed
    // reply ids fail the call explicitly; neither path converts an id to an integer.
    // timeoutMs < 0 waits as long as the response takes: silence (a solver factorizing
    // for minutes) never fails it; only abortPending() or peer EOF ends it. >= 0 bounds
    // the WHOLE wait (pushes on the way do not extend it). A timeout is a transport
    // failure (false, err only, no {code} in result) and invalidates the connection
    // (the response is still in flight on the wire) — the next call() reconnects,
    // mirroring daemon_bridge.py's reconnect-on-EOF, so a stale reply is never consumed.
    // `onEvent`, when set, is invoked LIVE (on the calling thread, mid-call) for each
    // {"event":"agent.event",...} push that arrives while awaiting the response — so a blocking
    // agent run streams its events instead of buffering them until the call returns. Other event
    // kinds (doc.*) still queue for drainEvents() unless Options::discardUnconsumedEvents.
    // Empty = today's buffer-only behavior.
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err, int timeoutMs = -1,
              const std::function<void(const nlohmann::json&)>& onEvent = {});

    // Move any queued push events out (non-blocking; also polls the socket).
    std::vector<nlohmann::json> drainEvents();

    // Monotonic counter bumped on every successful reconnect (EOF/timeout drop
    // followed by a fresh connect + hello). Each reconnect lands on a NEW daemon
    // clientId, so per-connection daemon state (event subscriptions especially)
    // is gone — a client that subscribed once must re-subscribe when this
    // advances. Tracking the daemon pid is NOT enough: a reconnect can land on
    // the same daemon (transient EOF, drain-replace race) with the pid unchanged
    // but the subscription already dropped. Safe to read from any thread.
    uint64_t reconnectGeneration() const { return reconnectGen_.load(); }

    // Proactive reconnect for the GUI frame loop when the socket is dead (daemon
    // crash, --stop, network drop). Same connect-only + hello path as the
    // call()-driven reconnect; never spawns. False when the peer is still gone or
    // refuses hello (helloError() names why; caller applies its own backoff). Bumps
    // reconnectGeneration() on success.
    bool tryReconnect(std::string& err);

    // Non-blocking liveness probe for the GUI frame loop. Drains any pending
    // event lines into the queue (same as drainEvents) and surfaces peer death
    // (HUP/EOF) so connected() becomes false without waiting for the next RPC.
    // try_locks — never stalls the render thread behind a long call().
    bool pollAlive();

    // Non-blocking reconnect for the wasm GUI frame loop. Acquires mtx_ with
    // try_to_lock ONCE on the render thread; if a call() is already in flight it
    // returns false immediately (skip this tick) rather than hard-blocking.
    //
    // Why this exists: under ASYNCIFY (no PROXY_TO_PTHREAD) the browser WebSocket
    // onclose/onmessage callbacks run on the MAIN (render) thread. If the render
    // thread hard-blocks in a lock_guard(mtx_) behind an AsyncRpc-worker call that
    // is stuck reading a dead socket, that socket's onclose can never fire to set
    // eof — the worker never returns, mtx_ is never released: permanent freeze
    // (the "page stops responding" seen after a daemon restart). Because the lock
    // is taken only via try_to_lock and the whole connect+hello runs inline under
    // that single held lock (never a second blocking acquire), the render thread
    // can never stall on it. Bootstraps sockPath_ on first use so it works for the
    // initial connect too. Bumps reconnectGeneration() on success. Never spawns.
    // *deferred (optional) is set true when the attempt was skipped because a
    // call() held mtx_ (not a failure — retry next frame without backoff).
    bool tryReconnectNonBlocking(std::string& err, bool* deferred = nullptr);

private:
    bool connectInner(const Options& opts, std::string& err);
    bool tryConnect(std::string& err);
    // Close the connection the daemon closes after it refuses hello, and name the
    // refusal (code, message, hint) in err.
    void refuseHello(const nlohmann::json& error, std::string& err);
    void setHelloError(nlohmann::json error);
    // `refusal` receives the daemon's hello error object, if it answered with one.
    bool hello(const std::string& kind, std::string& err, nlohmann::json& refusal);
    // Re-establish a connection invalidated by a timeout/EOF (mtx_ held by the
    // caller). Connect-only — never spawns — plus an inline hello re-handshake.
    bool reconnectLocked(std::string& err);
    // Read lines until a response with `id` arrives; queue (or, on a discarding
    // lane, drop) event lines seen on the way. timeoutMs >= 0 bounds the whole
    // wait; on timeout the connection is closed (mid-response, unusable) and
    // false/err returned. timeoutMs < 0 waits until the response, EOF or abort.
    bool readResponse(int id, nlohmann::json& out, std::string& err, int timeoutMs,
                      const std::function<void(const nlohmann::json&)>& onEvent = {});
    // Queue an unconsumed push for drainEvents(), or drop it on a discarding lane.
    void retainEvent(nlohmann::json&& msg);
    EngineClient* backgroundClient();
    void pollBackgroundClients();
    void closeConnection();

    IpcConn conn_;
    std::mutex mtx_;                     // primary round trips + event queue access
    std::mutex socketMtx_;               // socket replacement/close vs teardown shutdown
    std::vector<nlohmann::json> events_;
    nlohmann::json serverInfo_;
    mutable std::mutex helloErrMtx_;     // guards helloError_ only
    nlohmann::json helloError_;          // see helloError()
    std::string sockPath_;
    std::string clientKind_;             // saved for the reconnect re-handshake
    std::string userId_;                 // saved with clientKind_ for reconnect hello
    std::string userDisplay_;
    bool discardEvents_ = false;         // Options::discardUnconsumedEvents
    int nextId_ = 1;
    std::atomic<bool> abort_{false};     // set by abortPending(); readResponse returns fast
    std::atomic<uint64_t> reconnectGen_{0};  // bumped by reconnectLocked (see accessor)
    std::atomic<bool> connected_{false};
    std::thread::id primaryThread_ = std::this_thread::get_id();
    // Held only for slot/config access, never a connect/hello/RPC round trip.
    std::mutex backgroundMtx_;
    std::map<std::thread::id, std::unique_ptr<EngineClient>> backgroundClients_;
};

}  // namespace chudware_ipc
