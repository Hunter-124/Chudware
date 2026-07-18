// EngineClient — a client connection to the shared engine-host daemon.
//
// Drop-in sibling of core::CppCore (services/worker/cpp_core.h): same blocking
// call() contract, so the GUI's routing seam (worker_client.cpp) and tests can
// swap backends without signature churn. Adds connect-or-spawn discovery and a
// queue of daemon push events (doc.changed etc., drained by the GUI per frame).
//
// Threading: call() is serialized by an internal mutex (one outstanding request
// per connection, matching the daemon's v1 contract). Event lines that arrive
// while waiting for a response are queued; drainEvents() also does a 0 ms poll
// so pushes arrive even when the client is idle.
#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "ipc_transport.h"

namespace aicad_ipc {

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
    };

    // Connect to the daemon for this instance key, spawning one if absent (and
    // allowed). Performs the hello handshake. False + err on failure.
    bool connectOrSpawn(const Options& opts, std::string& err);

    bool connected() const { return conn_.valid() && !conn_.eof(); }
    void disconnect() { conn_.close(); }

    // The hello response: {proto, abi, engine_path, git_rev, daemon_pid, state, id_remap}.
    const nlohmann::json& serverInfo() const { return serverInfo_; }

    // Blocking JSON-RPC round trip; same contract as CppCore::call. Returns true
    // iff the daemon answered with "result"; err carries transport or RPC errors
    // ("error" payloads are also copied into result for callers that inspect them).
    // timeoutMs < 0 blocks forever; >= 0 bounds the wait for the response. A
    // timeout is a transport failure (false, err only, no {code} in result) and
    // invalidates the connection (the response is still in flight on the wire) —
    // the next call() reconnects, mirroring daemon_bridge.py's reconnect-on-EOF.
    // `onEvent`, when set, is invoked LIVE (on the calling thread, mid-call) for each
    // {"event":"agent.event",...} push that arrives while awaiting the response — so a blocking
    // agent run streams its events instead of buffering them until the call returns. Other event
    // kinds (doc.*) still queue for drainEvents(). Empty = today's buffer-only behavior.
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
    // call()-driven reconnect; never spawns. False when the peer is still gone
    // (caller applies its own backoff). Bumps reconnectGeneration() on success.
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
    bool hello(const std::string& kind, std::string& err);
    // Re-establish a connection invalidated by a timeout/EOF (mtx_ held by the
    // caller). Connect-only — never spawns — plus an inline hello re-handshake.
    bool reconnectLocked(std::string& err);
    // Read lines until a response with `id` arrives; queue event lines seen on
    // the way. timeoutMs bounds the whole wait; on timeout the connection is
    // closed (mid-response, unusable) and false/err returned.
    bool readResponse(int id, nlohmann::json& out, std::string& err, int timeoutMs,
                      const std::function<void(const nlohmann::json&)>& onEvent = {});

    IpcConn conn_;
    std::mutex mtx_;                     // serialize call()s + event queue access
    std::vector<nlohmann::json> events_;
    nlohmann::json serverInfo_;
    std::string sockPath_;
    std::string clientKind_;             // saved for the reconnect re-handshake
    std::string userId_;                 // saved with clientKind_ for reconnect hello
    std::string userDisplay_;
    int nextId_ = 1;
    std::atomic<uint64_t> reconnectGen_{0};  // bumped by reconnectLocked (see accessor)
};

}  // namespace aicad_ipc
