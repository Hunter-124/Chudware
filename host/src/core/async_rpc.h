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
#include <chrono>
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

// hostd's agent worker lane is held by a running agent turn; agent-lane service calls
// (omp_*, subscription_*) fail fast with this code instead of waiting hours. Transient
// and internal: callers keep what they last showed and retry after the turn.
inline bool workerLaneBusy(const RpcResult& r) {
    if (!r.result.is_object()) return false;
    const auto it = r.result.find("code");
    return it != r.result.end() && it->is_string() && *it == "worker_lane_busy";
}

// The refusal came from an interactive sign-in (omp_login) holding hostd's service worker, not
// from an agent run. Same transient wait; only the words shown to the user differ.
inline bool workerLaneBusyBySignIn(const RpcResult& r) {
    if (!workerLaneBusy(r)) return false;
    const auto it = r.result.find("message");
    return it != r.result.end() && it->is_string() &&
           it->get<std::string>().find("sign-in") != std::string::npos;
}

// The service has no such method (an older worker or runtime). STRICT by design: callers latch
// this for the rest of the session, so only the explicit unknown-method spellings count. A
// transport failure that merely mentions "method" or "not found" (hostd's binary missing for
// the moment a rebuild replaces it) is a transient outage, never an absent RPC.
inline bool workerMethodAbsent(const RpcResult& r) {
    const auto namesUnknown = [](const std::string& text) {
        for (const char* needle : {"unknown_method", "unknown method", "method_not_found",
                                   "method not found", "not_implemented"})
            if (text.find(needle) != std::string::npos) return true;
        return false;
    };
    if (!r.ok) return namesUnknown(r.err);
    if (!r.result.is_object()) return false;
    const auto field = [&r](const char* key) {
        const auto it = r.result.find(key);
        return it != r.result.end() && it->is_string() ? it->get<std::string>() : std::string();
    };
    const std::string code = field("code");
    if (code == "unknown_method" || code == "method_not_found" || code == "not_implemented")
        return true;
    // hostd answers a worker that lacks the method as {code:"worker_error",
    // message:"unknown method: <name>"}.
    if (code == "worker_error" && field("message").rfind("unknown method:", 0) == 0)
        return true;
    const auto ok = r.result.find("ok");
    if (ok != r.result.end() && ok->is_boolean() && !ok->get<bool>()) {
        const std::string reason = field("reason");
        return reason == "unknown_method" || reason == "method_not_found" ||
               reason == "not_implemented";
    }
    return false;
}

// Client deadline for engine calls hostd runs in its long-running watchdog tier (doc_open,
// doc_activate, doc_save, …: core/hostd/engine_watchdog.h; default 60 min, env
// CHUDWARE_HOSTD_ENGINE_WATCHDOG_LONG_S). A shorter client deadline reports `Open failed`
// while the engine is still legitimately compiling under the doc lock; hostd's watchdog is
// what bounds a genuinely stuck call.
inline constexpr int kLongEngineCallTimeoutMs = 60 * 60 * 1000;

class AsyncRpc {
public:
    using Done = std::function<void(const RpcResult&)>;
    // Streamed mid-call progress (worker "event" frames). Delivered on the GL thread inside
    // drain(), in arrival order, always before the owning call's Done. Copied per event.
    using Event = std::function<void(const nlohmann::json&)>;
    // The primary transport must observe terminal cancellation before reconnect/send.
    using PrimaryCall = bool (*)(const std::string&, const nlohmann::json&,
                                nlohmann::json&, std::string&, int,
                                const std::atomic<bool>&);

    explicit AsyncRpc(WorkerClient& w) : worker_(w) {}
    ~AsyncRpc() { shutdown(); }

    // Queue a call. done runs inside a later drain() on the GL thread (or never, if
    // shutdown() drops it first). The dispatch thread starts lazily on the first call.
    void call(std::string method, nlohmann::json params, Done done, int timeoutMs = 120000);
    // Same, with a mid-call event stream (e.g. omp_login's open_url). onEvent runs
    // inside drain() on the GL thread like done — never on the dispatch thread.
    void call(std::string method, nlohmann::json params, Done done, Event onEvent,
              int timeoutMs = 120000);

    // Same FIFO, but retain the primary engine connection's identity for attach/subscribe
    // operations. No WorkerClient side-connection fallback. Configure the transport at GUI
    // startup; an unavailable primary reports an explicit transport failure.
    void callPrimary(std::string method, nlohmann::json params, Done done,
                     int timeoutMs = 120000);
    void setPrimaryCaller(PrimaryCall call) { primaryCall_.store(call); }

    // Latest-wins edits retain their first FIFO position (so a later save cannot overtake
    // them). Only queued calls with the same method+key are replaced; an in-flight call is
    // never changed. The superseded callback is dropped, not invoked. For nonzero debounceMs,
    // due = min(latest replacement + debounceMs, first enqueue + 2*debounceMs). This bounds
    // the debounce hold on later FIFO calls even during a continuous drag; they cannot
    // overtake the pending edit. Prior in-flight RPCs can still delay the whole queue.
    void callCoalesced(std::string method, nlohmann::json params, Done done,
                       std::string key, int timeoutMs = 120000, int debounceMs = 0);

    // Explicit save/close barrier, GL thread only: dispatch a matching debounced edit
    // immediately, wait for its queued/in-flight calls, then drain their callbacks.
    // Other FIFO work ahead of the edit also completes; never use in a frame path.
    void flushCoalesced(const std::string& method, const std::string& key);

    // Exit barrier, GL thread only: run callbacks and wait until no call is queued, executing or
    // undrained, for at most maxWaitMs. Returns whether the queue went idle. Use it once, just
    // before shutdown(), to let already-sent final edits (e.g. ai_roles_set) land — shutdown()
    // would abort the in-flight one. Never use in a frame path.
    bool drainUntilIdle(int maxWaitMs);

    // Run the callbacks of every finished call. Once per frame, GL thread.
    void drain();

    // Optional thread-safe GUI-loop wake, called after progress/results are queued. Kept as
    // a function pointer so non-GUI users do not depend on ImGui/GLFW.
    void setWakeCallback(void (*wake)()) { onWake_.store(wake); }

    // True while any call is queued, executing, or finished-but-undrained.
    bool busy() const { return pending_.load() > 0; }
    // Metadata/background polling must not disable a setup gesture or pin the GUI to vsync.
    // Explicitly classified reference-mesh and ECAD hub reads are background; other
    // document reads, writes and unknown methods remain interaction-relevant.
    bool interactionBusy() const { return interactionPending_.load() > 0; }

    // Stop the dispatch thread: drop queued + undrained work, abort the in-flight call,
    // join. Idempotent; also run by the destructor.
    void shutdown();

private:
    struct Pending {
        std::string method;
        std::string coalesceKey;
        nlohmann::json params;
        int timeoutMs = 120000;
        std::chrono::steady_clock::time_point readyAt{};
        std::chrono::steady_clock::time_point firstQueued{};
        Done done;
        Event onEvent;
        RpcResult res;
        bool blocksInteraction = true;
        bool primary = false;
    };

    void threadMain();
    void callImpl(std::string method, nlohmann::json params, Done done, Event onEvent,
                  int timeoutMs, std::string coalesceKey, bool primary = false,
                  int debounceMs = 0);
    void notifyReady() const {
        if (const auto wake = onWake_.load()) wake();
    }

    WorkerClient& worker_;
    std::thread thread_;
    bool threadStarted_ = false;

    std::mutex mtx_;                                // guards queue_ / doneQ_
    std::condition_variable cv_;
    std::deque<std::unique_ptr<Pending>> queue_;    // waiting to run (FIFO)
    Pending* inFlight_ = nullptr;                  // borrowed from dispatch thread, guarded by mtx_
    std::vector<std::unique_ptr<Pending>> doneQ_;   // finished, awaiting drain()
    // Streamed events awaiting drain() (Event copied per entry — the owning Pending may be
    // drained in the same pass; the copy keeps delivery self-contained).
    std::vector<std::pair<Event, nlohmann::json>> eventQ_;
    std::atomic<bool> quit_{false};
    std::atomic<int> pending_{0};                   // queued + executing + undrained
    std::atomic<int> interactionPending_{0};
    std::atomic<void (*)()> onWake_{nullptr};
    std::atomic<PrimaryCall> primaryCall_{nullptr};
};

}  // namespace core
