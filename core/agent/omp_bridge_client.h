// core/agent/omp_bridge_client.h — long-lived OMP bridge sidecar client.
//
// core/agent/providers.cpp::complete() forwards OMP-backed providers (see
// is_omp_bridge_provider() in providers.h) here instead of building a native libcurl/WinHTTP
// request. Packaged builds launch only the canonical adjacent chudware-omp-bridge binary;
// developer builds retain CHUDWARE_OMP_BRIDGE_BIN or the Bun source bridge.
// The client speaks the newline-delimited JSON-RPC protocol documented at the top of bridge.ts:
//   -> {"id": <int>, "method": <string>, "params": <object>}
//   <- {"id": <int>, "ok": true,  "result": <object>}
//   <- {"id": <int>, "ok": false, "error": {"message": <string>, "status"?: <number>}}
//   <- {"id": <int>, "event": <string>, "data": <object>}   (notification; no "ok" member)
// Notification frames are opt-in per request (today: `complete` with params.stream=true, which
// emits {"event":"delta","data":{"text"|"thinking":<chunk>}} per fragment) and always precede
// that id's response line. They are handed to the call's OnEvent sink and NEVER complete or
// retire the request. stdout carries ONLY those frames; the child's stderr is pumped back to
// this process's stderr while retaining a bounded diagnostic tail.
//
// Concurrency: the stdio channel is multiplexed, not serialized. A background reader thread
// owns all reads off the child's stdout and dispatches each response to its id-matched caller;
// each call() only ever holds a lock long enough to write its own request line. Two threads
// calling call() concurrently (e.g. two independent chat sessions both routed through a
// bridge-backed provider) make independent progress — neither blocks behind the other's full
// round-trip.
//
// Mirrors core/hostd/worker_bridge.{h,cpp} in transport shape: a pipe-backed child,
// id-correlated call(), and reap-and-respawn on a dead child. Unlike worker_bridge,
// request/response correlation is not serialized behind one lock. POSIX uses fork/exec;
// Windows uses CreateProcessW with inherited stdio pipes.
#pragma once

#include <atomic>
#include <functional>
#include <mutex>
#include <string>

#include <nlohmann/json.hpp>

#include <condition_variable>
#include <cstdint>
#include <memory>
#include <thread>
#include <unordered_map>

#ifndef _WIN32
#include <sys/types.h>
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

    // Resolve the packaged bridge binary or developer Bun/script paths once. Caller holds
    // mtx_.
    bool resolvePaths(std::string& err);
    // Bring <vendorOmp>/node_modules in line with the committed bun.lock before spawning. Runs
    // per spawn (not once per process) so a checkout pulled forward under a live daemon heals
    // on the next respawn instead of dying at import on every call; memoized on the lockfile
    // fingerprint so one failed install is not retried on every provider poll. Caller holds
    // mtx_.
    void provisionDeps();
    bool spawn(std::string& err);  // caller holds mtx_
    // Caller holds mtx_. Tears down the process and pipes, then fails every outstanding
    // Pending with a transport error. Whichever thread notices death is the single place
    // that guarantees no caller is left hanging.
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
    // Background reader thread body for one generation: pumps response lines until EOF/error,
    // dispatches each to its id-matched Pending, then reaps this generation and fails whatever
    // remains outstanding. The process identity is captured at spawn time rather than re-read
    // from fields that may already name a newer generation.
#ifdef _WIN32
    void readerLoop(void* fd, void* process);
    void stderrLoop(void* fd);
#else
    void readerLoop(int fd, pid_t pid);
    void stderrLoop(int fd);
#endif
    // Last few non-empty stderr lines of the most recent child, condensed onto one line.
    std::string stderrSummary();
    void dispatchResponse(const std::string& line);  // caller holds no lock
    void failAllPending(const std::string& reason);   // caller holds no lock

#ifdef _WIN32
    void* childStdinWr_ = nullptr;   // HANDLE: write -> child stdin
    void* childStdoutRd_ = nullptr;  // HANDLE: read <- child stdout
    void* job_ = nullptr;            // HANDLE: kill-on-close containment job
    void* process_ = nullptr;        // HANDLE
#else
    int childStdinWr_ = -1;   // write -> child stdin
    int childStdoutRd_ = -1;  // read  <- child stdout
    pid_t process_ = -1;
#endif
    // Resolved once on first spawn. Packaged builds derive the canonical adjacent bridge
    // from the current executable and reject non-adjacent inherited overrides. Developer
    // builds retain CHUDWARE_OMP_BRIDGE_BIN, CHUDWARE_BUN, and the source-script override.
    std::string bunExe_;
    std::string bridgeScript_;
    std::string bridgeCwd_;
    bool compiledBridge_ = false;
    // <...>/vendor/omp — the vendored bun workspace provisionDeps() keeps current.
    std::string vendorOmpDir_;
    // Lockfile fingerprint provisionDeps() last acted on in this process (see provisionDeps).
    std::string depsStamp_;
    bool pathsResolved_ = false;
    bool running_ = false;

    // Background reader for the current generation's child. Joined (never detached) before a
    // respawn (ensureRunning) and on shutdown (stop()) so no thread outlives its stdio fds.
    std::thread reader_;
    // Owns the current generation's stderr pipe end; joined in reap(). Never detached.
    std::thread stderrPump_;
    // Rolling tail of the current generation's stderr, guarded independently of mtx_ (reap()
    // holds mtx_ while joining the pump, which must never need it).
    static constexpr size_t kMaxStderrTail = 4096;
    std::mutex stderrMtx_;
    std::string stderrTail_;

    // Request id -> in-flight caller. Guarded independently of mtx_: the reader thread only
    // ever needs this map plus each Pending's own mutex, never the process/fd state guarded by
    // mtx_, so dispatching a response never contends with a concurrent spawn/write.
    std::mutex pendingMtx_;
    std::unordered_map<int64_t, std::shared_ptr<Pending>> pending_;
    std::atomic<int64_t> nextId_{1};

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
