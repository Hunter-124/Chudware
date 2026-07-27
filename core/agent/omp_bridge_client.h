// core/agent/omp_bridge_client.h — long-lived bun bridge sidecar client
// (vendor/omp/bridge/src/bridge.ts).
//
// core/agent/providers.cpp::complete() forwards OMP-backed providers (see
// is_omp_bridge_provider() in providers.h) here instead of building a native libcurl/WinHTTP
// request. This owns ONE `bun run <repo>/vendor/omp/bridge/src/bridge.ts` child process and
// speaks the newline-delimited JSON-RPC protocol documented at the top of bridge.ts:
//   -> {"id": <int>, "method": <string>, "params": <object>}
//   <- {"id": <int>, "ok": true,  "result": <object>}
//   <- {"id": <int>, "ok": false, "error": {"message": <string>, "status"?: <number>}}
//   <- {"id": <int>, "event": <string>, "data": <object>}   (notification; no "ok" member)
// Notification frames are opt-in per request (today: `complete` with params.stream=true, which
// emits {"event":"delta","data":{"text"|"thinking":<chunk>}} per fragment) and always precede
// that id's response line. They are handed to the call's OnEvent sink and NEVER complete or
// retire the request. stdout carries ONLY those frames; the child's own stderr logging is left
// connected to this process's stderr (nothing redirected/discarded).
//
// Concurrency: the stdio channel is multiplexed, not serialized. A background reader thread
// owns all reads off the child's stdout and dispatches each response to its id-matched caller;
// each call() only ever holds a lock long enough to write its own request line. Two threads
// calling call() concurrently (e.g. two independent chat sessions both routed through a
// bridge-backed provider) make independent progress — neither blocks behind the other's full
// round-trip.
//
// Mirrors core/hostd/worker_bridge.{h,cpp} (the C++ core's existing Python-worker stdio
// bridge) in transport shape: a fork+pipe POSIX child, id-correlated call(), reap-and-respawn
// on a dead child. Unlike worker_bridge, request/response correlation is NOT serialized behind
// one lock — see the reader-thread design above. Windows is currently an "unavailable" stub,
// matching worker_bridge's own Windows branch — the bun bridge does not yet have a
// CreateProcess-based child transport.
#pragma once

#include <atomic>
#include <functional>
#include <mutex>
#include <string>

#include <nlohmann/json.hpp>

#ifndef _WIN32
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <sys/types.h>
#include <thread>
#include <unordered_map>
#endif

namespace chudware {
namespace agent {

class OmpBridgeClient {
public:
    static OmpBridgeClient& instance();

    // Optional per-call sink for the bridge's notification frames (see the protocol block
    // above): onEvent(data, event) fires once per {"id","event","data"} line arriving on this
    // call's id, before its response. Invoked on the background reader thread, but serialized
    // against the call's own teardown — the sink is guaranteed never to fire before call()
    // registers it nor after call() returns, so a lambda capturing the caller's stack (the
    // run's thread_local delta emitter, say) is safe to pass.
    using OnEvent = std::function<void(const nlohmann::json& data, const std::string& event)>;

    // Send {method, params} to the bun bridge; block until the matching response (default
    // 120 s, matching worker_bridge's default — a `complete` round-trip through a reasoning
    // model can legitimately take a while even when its deltas stream in via onEvent;
    // providers.cpp passes an explicit larger timeout for that call).
    //
    // Safe to call concurrently from multiple threads: each call gets its own id-correlated
    // response and blocks only on ITS OWN result, never on another in-flight call's.
    //
    // Returns true on ok:true (result filled with the bridge's `result` object). Returns false
    // on transport failure or ok:false; err is always set on false, and result carries the
    // bridge's structured {message,status?} error object when it supplied one.
    bool call(const std::string& method, const nlohmann::json& params, nlohmann::json& result,
              std::string& err, int timeoutMs = 120000, OnEvent onEvent = nullptr);

    // Best-effort shutdown of the child (idempotent). Called from ~OmpBridgeClient. Joins the
    // background reader thread before returning — no thread outlives the child's fds.
    void stop();

    // Stops the singleton's child WITHOUT constructing it if instance() was never called —
    // safe to call unconditionally from hostd's clean-shutdown tail after server.run()
    // returns (does NOT lazily spin up a bridge process just to shut it down).
    static void shutdownIfCreated();

private:
    OmpBridgeClient() = default;
    ~OmpBridgeClient();
    OmpBridgeClient(const OmpBridgeClient&) = delete;
    OmpBridgeClient& operator=(const OmpBridgeClient&) = delete;

#ifndef _WIN32
    // One caller's outstanding request: the reader thread fills {ok,result,err} and flips
    // `done` under `mtx`, notifying `cv`. The waiting call() thread owns `mtx` for the whole
    // wait (condition_variable::wait_for releases/reacquires it around each sleep), so reads
    // of {done,ok,result,err} after wait_for returns are always synchronized with the write.
    struct Pending {
        std::mutex mtx;
        std::condition_variable cv;
        bool done = false;
        bool ok = false;
        nlohmann::json result;
        std::string err;
        // Notification sink for this id. Written once at registration, before the request line
        // goes out; read-only afterwards. The reader thread invokes it under `mtx` (never under
        // pendingMtx_) — see dispatchResponse().
        OnEvent onEvent;
        // Flipped by the waiting caller under `mtx` the instant it stops waiting, whether that
        // is normal completion, timeout, or cancel. The reader thread refuses to invoke
        // `onEvent` once this is set, which is what lets the sink capture the caller's stack.
        bool detached = false;
    };

    // Resolve bunExe_/bridgeScript_ once (env overrides, else repo-root discovery). Caller
    // holds mtx_.
    bool resolvePaths(std::string& err);
    bool spawn(std::string& err);  // caller holds mtx_
    // Caller holds mtx_. Tears down process_/fds (fast WNOHANG check, SIGTERM+blocking-wait
    // fallback if still alive) and fails every outstanding Pending with a transport error —
    // whichever thread notices the child is dead (a caller's failed write, or the reader
    // thread's EOF), this is the single place that guarantees no one is left hanging.
    void reap();
    bool writeLine(const std::string& line);
    // Ensures the current generation's child + its background reader thread are running,
    // (re)spawning if the previous generation died. Joins the previous reader thread (if any)
    // before spawning a new child, so exactly one reader thread ever owns a given stdout fd.
    bool ensureRunning(std::string& err);
    // One attempt against the CURRENT generation: registers a Pending under `id`, writes the
    // request line (mtx_ held only for the write itself), then blocks on the Pending's own
    // condition_variable until the reader thread fills it, `timeoutMs` elapses, or the run's
    // provider-cancel flag flips. Sets childDead=true when the failure means the whole
    // transport died (write failure, or the reader thread failing this Pending because the
    // subprocess exited) — call() only respawns-and-retries in that case; a plain timeout,
    // cancellation, or an ok:false method-level RPC error is final.
    bool callOnce(const std::string& method, const nlohmann::json& params, nlohmann::json& result,
                  std::string& err, int timeoutMs, const OnEvent& onEvent, bool& childDead);
    // Background reader thread body for one generation: pumps response lines off `fd` until
    // EOF/error, dispatching each to its id-matched Pending, then reaps this generation (if it
    // wasn't already reaped by a concurrent caller) and fails whatever is left outstanding so
    // no caller hangs. `fd`/`pid` are captured at spawn time rather than re-read from
    // childStdoutRd_/process_, which may already name a newer generation by the time this
    // thread notices its own child died.
    void readerLoop(int fd, pid_t pid);
    void dispatchResponse(const std::string& line);  // caller holds no lock
    void failAllPending(const std::string& reason);   // caller holds no lock

    int childStdinWr_ = -1;   // write -> child stdin
    int childStdoutRd_ = -1;  // read  <- child stdout (owned by the reader thread once spawned)
    pid_t process_ = -1;
    bool running_ = false;
    // Resolved once on first spawn. Defaults to a PATH-searched "bun" (or a common
    // curl-installer location) and <repo root>/vendor/omp/bridge/src/bridge.ts; both are
    // overridable (CHUDWARE_BUN, CHUDWARE_OMP_BRIDGE) for checkouts/installs that don't sit under
    // $HOME/Repositories/CAD or don't have bun on PATH.
    std::string bunExe_;
    std::string bridgeScript_;
    bool pathsResolved_ = false;

    // Background reader for the current generation's child. Joined (never detached) before a
    // respawn (ensureRunning) and on shutdown (stop()) so no thread outlives its stdio fds.
    std::thread reader_;

    // Request id -> in-flight caller. Guarded independently of mtx_: the reader thread only
    // ever needs this map plus each Pending's own mutex, never the process/fd state guarded by
    // mtx_, so dispatching a response never contends with a concurrent spawn/write.
    std::mutex pendingMtx_;
    std::unordered_map<int64_t, std::shared_ptr<Pending>> pending_;
    std::atomic<int64_t> nextId_{1};
#endif

    // Guards process_/childStdinWr_/childStdoutRd_/running_/pathsResolved_ and serializes
    // writing one request line to the child's stdin. Held only for spawn/reap/write — never
    // across a response wait — so concurrent call()s from different threads each block only on
    // their own response once their request is written.
    std::mutex mtx_;

    // Set true the first time instance() runs; lets shutdownIfCreated() avoid constructing
    // the function-local static singleton just to no-op stop() on it.
    static std::atomic<bool> created_;
};

}  // namespace agent
}  // namespace chudware
