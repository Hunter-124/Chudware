// AsyncRpc (C39): non-blocking worker calls for the GL/UI thread.
//
// WorkerClient::call() is a mutex-serialized blocking round-trip; calling it from the GL
// thread freezes the window for the call's duration (doc_rebuild after an agent run or an
// eval_* on a heavy model can take seconds to a minute). AsyncRpc moves the round-trip onto
// ONE persistent dispatch thread and delivers the result back on the GL thread:
//
//     st.rpc.call("eval_mass", params, [&st](const core::RpcResult& r) { /* apply */ });
//     ...
//     st.rpc.drain();   // once per frame, top of the main loop — runs finished callbacks
//
// Contract:
//   - call() / drain() / shutdown() are GL-thread-only. Callbacks run ONLY inside drain()
//     (GL thread), so they may touch AppState / GL / the renderer freely.
//   - Calls dispatch FIFO on a single thread — the same serialization WorkerClient::callMtx_
//     enforces anyway, without parking the UI on it. Queue order is preserved.
//   - A SYNC call made from the GL thread while an async call is in flight still blocks on
//     the WorkerClient mutex until that call finishes. Keep latency-critical sync paths
//     (sketch_drag) clear of long async work, or check busy() first.
//   - busy() stays true until a result has been DRAINED (not merely received), so headless
//     shot scenarios can gate a capture on !busy() and know the frame shows every result.
//   - shutdown() drops queued + undrained callbacks unrun, aborts the in-flight call via
//     WorkerClient::abortPending(), and joins. Call it before WorkerClient::stop().
#pragma once

#include <atomic>
#include <condition_variable>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

class WorkerClient;

namespace core {

// One finished round-trip, same contract as WorkerClient::call(): ok = transport success
// (protocol-2 method errors arrive ok=true with result.code set — callers keep checking).
struct RpcResult {
    bool ok = false;
    nlohmann::json result;
    std::string err;
};

class AsyncRpc {
public:
    using Done = std::function<void(const RpcResult&)>;
    // Streamed mid-call progress (worker "event" frames). Delivered on the GL thread inside
    // drain(), in arrival order, always before the owning call's Done. Copied per event.
    using Event = std::function<void(const nlohmann::json&)>;

    explicit AsyncRpc(WorkerClient& w) : worker_(w) {}
    ~AsyncRpc() { shutdown(); }

    // Queue a call. done runs inside a later drain() on the GL thread (or never, if
    // shutdown() drops it first). The dispatch thread starts lazily on the first call.
    void call(std::string method, nlohmann::json params, Done done, int timeoutMs = 120000);
    // Same, with a mid-call event stream (e.g. chudcode_login's open_url). onEvent runs
    // inside drain() on the GL thread like done — never on the dispatch thread.
    void call(std::string method, nlohmann::json params, Done done, Event onEvent,
              int timeoutMs = 120000);

    // Run the callbacks of every finished call. Once per frame, GL thread.
    void drain();

    // True while any call is queued, executing, or finished-but-undrained.
    bool busy() const { return pending_.load() > 0; }

    // Stop the dispatch thread: drop queued + undrained work, abort the in-flight call,
    // join. Idempotent; also run by the destructor.
    void shutdown();

private:
    struct Pending {
        std::string method;
        nlohmann::json params;
        int timeoutMs = 120000;
        Done done;
        Event onEvent;
        RpcResult res;
    };

    void threadMain();

    WorkerClient& worker_;
    std::thread thread_;
    bool threadStarted_ = false;

    std::mutex mtx_;                                // guards queue_ / doneQ_ / quit_
    std::condition_variable cv_;
    std::deque<std::unique_ptr<Pending>> queue_;    // waiting to run (FIFO)
    std::vector<std::unique_ptr<Pending>> doneQ_;   // finished, awaiting drain()
    // Streamed events awaiting drain() (Event copied per entry — the owning Pending may be
    // drained in the same pass; the copy keeps delivery self-contained).
    std::vector<std::pair<Event, nlohmann::json>> eventQ_;
    bool quit_ = false;
    std::atomic<int> pending_{0};                   // queued + executing + undrained
};

}  // namespace core
