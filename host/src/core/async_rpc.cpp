// AsyncRpc (C39) implementation — see async_rpc.h.
#include "async_rpc.h"

#include <algorithm>
#include <cstdio>
#include <string_view>

#include "../worker_client.h"
#include "../../../services/ipc/background_query.h"

namespace {
bool blocksInteraction(std::string_view method, const nlohmann::json& params) {
    // Exact background methods only. Never infer safety from a "get"/"list" prefix:
    // all other document reads and every unknown method stay blocking.
    (void)params;
    static constexpr std::string_view metadata[] = {
        "fw_projects", "agent_list_models", "subscription_accounts",
        "subscription_status", "subscription_usage", "omp_auth_state",
        "omp_models_list", "omp_config_get", "ai_prompt_baseline", "skills_list",
        "em_status", "sim_caps", "fw_status",
        "topology_capabilities", "omp_provider_list", "plm_timeline",
        "bench_status", "bench_categories", "doc_mesh_ref_list", "doc_mesh_ref_mesh",
        "ecad_tree", "ecad_kicad_status"
    };
    for (const auto name : metadata)
        if (method == name) return false;
    return true;
}
}  // namespace

namespace core {

void AsyncRpc::call(std::string method, nlohmann::json params, Done done, int timeoutMs) {
    call(std::move(method), std::move(params), std::move(done), Event{}, timeoutMs);
}

void AsyncRpc::call(std::string method, nlohmann::json params, Done done, Event onEvent,
                    int timeoutMs) {
    callImpl(std::move(method), std::move(params), std::move(done), std::move(onEvent),
             timeoutMs, {});
}

void AsyncRpc::callPrimary(std::string method, nlohmann::json params, Done done,
                          int timeoutMs) {
    callImpl(std::move(method), std::move(params), std::move(done), Event{},
             timeoutMs, {}, true);
}

void AsyncRpc::callCoalesced(std::string method, nlohmann::json params, Done done,
                             std::string key, int timeoutMs, int debounceMs) {
    callImpl(std::move(method), std::move(params), std::move(done), Event{},
             timeoutMs, std::move(key), false, debounceMs);
}

void AsyncRpc::flushCoalesced(const std::string& method, const std::string& key) {
    if (key.empty()) return;
    const auto matches = [&](const Pending& p) {
        return p.method == method && p.coalesceKey == key;
    };
    {
        std::unique_lock<std::mutex> lk(mtx_);
        for (auto& p : queue_)
            if (matches(*p)) p->readyAt = {};
        cv_.notify_all();
        cv_.wait(lk, [&]() {
            return quit_.load() ||
                   ((!inFlight_ || !matches(*inFlight_)) &&
                    std::none_of(queue_.begin(), queue_.end(),
                                 [&](const auto& p) { return matches(*p); }));
        });
    }
    drain();
}

bool AsyncRpc::drainUntilIdle(int maxWaitMs) {
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(maxWaitMs < 0 ? 0 : maxWaitMs);
    for (;;) {
        drain();
        if (pending_.load() <= 0) return true;
        std::unique_lock<std::mutex> lk(mtx_);
        if (quit_.load()) return false;
        // A finishing call pushes to doneQ_ and notifies cv_ under mtx_, so checking the queues
        // under the same lock cannot miss a completion that raced with the drain above.
        if (!cv_.wait_until(lk, deadline, [&]() {
                return quit_.load() || !doneQ_.empty() || !eventQ_.empty();
            }))
            return false;  // timed out with work still pending
        if (quit_.load()) return false;
    }
}

void AsyncRpc::callImpl(std::string method, nlohmann::json params, Done done, Event onEvent,
                        int timeoutMs, std::string coalesceKey, bool primary, int debounceMs) {
    const bool blocking = blocksInteraction(method, params);
    auto p = std::make_unique<Pending>();
    p->blocksInteraction = blocking;
    p->primary = primary;
    p->method = std::move(method);
    p->coalesceKey = std::move(coalesceKey);
    if (!p->coalesceKey.empty() || debounceMs > 0)
        p->firstQueued = std::chrono::steady_clock::now();
    if (debounceMs > 0)
        p->readyAt = p->firstQueued + std::chrono::milliseconds(debounceMs);
    // `rpc.call("m", {}, cb)` looks like "no arguments" but nlohmann parses a bare `{}` as
    // NULL, not an empty object — and a null lands on the wire as `"params": null`. hostd's
    // `msg.value("params", json::object())` then returns the stored null (value() only
    // substitutes the default when the KEY is absent), so the first `params.value(...)` in a
    // handler throws type_error.306 and the daemon rejects the request. That silently broke
    // every zero-argument RPC — parts_taxonomy / parts_list_categories / vendor_status /
    // parts_queue_status / vendors_health_* / login_wizard_* — while calls that pass real
    // arguments worked fine. Normalize here so no null params can ever reach a handler.
    p->params = params.is_null() ? nlohmann::json::object() : std::move(params);
#ifndef __EMSCRIPTEN__
    const bool background = !primary && p->coalesceKey.empty() && debounceMs <= 0 &&
        chudware_ipc::isBackgroundQuery(p->method, p->params);
#else
    const bool background = false;  // the browser build keeps one dispatch thread
#endif
    p->timeoutMs = timeoutMs;
    p->done = std::move(done);
    p->onEvent = std::move(onEvent);
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (quit_) return;  // shutting down — drop silently (callbacks must not run after)
        if (!p->coalesceKey.empty()) {
            for (auto& queued : queue_) {
                if (queued->method == p->method && queued->coalesceKey == p->coalesceKey) {
                    p->firstQueued = queued->firstQueued;
                    if (debounceMs > 0)
                        p->readyAt = std::min(p->readyAt, p->firstQueued +
                            std::chrono::milliseconds(debounceMs) * 2);
                    if (queued->blocksInteraction != p->blocksInteraction)
                        interactionPending_.fetch_add(p->blocksInteraction ? 1 : -1);
                    queued = std::move(p);
                    cv_.notify_all();
                    return;
                }
            }
        }
        (background ? bgQueue_ : queue_).push_back(std::move(p));
        pending_.fetch_add(1);
        if (blocking) interactionPending_.fetch_add(1);
        // GL-thread-only by contract, so the lazy start can't race another call().
        bool& started = background ? bgThreadStarted_ : threadStarted_;
        if (!started) {
            started = true;
            (background ? bgThread_ : thread_) =
                std::thread([this, background]() { threadMain(background); });
        }
    }
    cv_.notify_all();  // both lanes wait on cv_
}

void AsyncRpc::threadMain(bool background) {
    auto& queue = background ? bgQueue_ : queue_;
    for (;;) {
        std::unique_ptr<Pending> p;
        {
            std::unique_lock<std::mutex> lk(mtx_);
            cv_.wait(lk, [this, &queue]() { return quit_ || !queue.empty(); });
            if (quit_) return;
            const auto readyAt = queue.front()->readyAt;
            if (readyAt != std::chrono::steady_clock::time_point{} &&
                readyAt > std::chrono::steady_clock::now()) {
                cv_.wait_until(lk, readyAt);
                continue;  // replacement, shutdown, or spurious wake: re-read the FIFO head
            }
            p = std::move(queue.front());
            queue.pop_front();
            // Only the document lane's in-flight call is a flushCoalesced barrier.
            if (!background) inFlight_ = p.get();
        }
        if (quit_.load()) return;  // shutdown may have begun immediately after the pop
        // The blocking round-trip happens here, off the GL thread. The errorSink (if any)
        // fires on THIS thread inside call() — it must stay thread-safe (it posts a toast,
        // and AppState::toast locks). Streamed event frames are queued under mtx_ and
        // delivered on the GL thread by drain(); they never run here.
        WorkerClient::EventFn ev;
        if (p->onEvent) {
            Pending* raw = p.get();   // alive until drained; events only fire mid-call
            ev = [this, raw](const nlohmann::json& data) {
                {
                    std::lock_guard<std::mutex> lk(mtx_);
                    if (quit_) return;
                    eventQ_.emplace_back(raw->onEvent, data);
                }
                notifyReady();
            };
        }
        if (p->primary) {
            if (quit_.load()) return;
            if (const auto call = primaryCall_.load())
                p->res.ok = call(p->method, p->params, p->res.result, p->res.err,
                                 p->timeoutMs, quit_);
            else
                p->res.err = "primary engine connection is unavailable";
        } else {
            p->res.ok = worker_.call(p->method, p->params, p->res.result, p->res.err, p->timeoutMs,
                                     ev);
        }
        {
            std::lock_guard<std::mutex> lk(mtx_);
            if (!background) inFlight_ = nullptr;
            if (quit_) return;  // result no longer wanted; pending_ is moot past shutdown
            doneQ_.push_back(std::move(p));
        }
        cv_.notify_all();
        notifyReady();
    }
}

void AsyncRpc::drain() {
    std::vector<std::pair<Event, nlohmann::json>> events;
    std::vector<std::unique_ptr<Pending>> done;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (doneQ_.empty() && eventQ_.empty()) return;
        done.swap(doneQ_);
        events.swap(eventQ_);
    }
    // Events first: they were emitted before their call's terminal result, and the swap above
    // is atomic with doneQ_, so an event can never arrive in a later drain than its Done.
    for (auto& e : events) {
        try {
            if (e.first) e.first(e.second);
        } catch (const std::exception& ex) {
            std::fprintf(stderr, "[async_rpc] event callback threw: %s\n", ex.what());
        } catch (...) {
            std::fprintf(stderr, "[async_rpc] event callback threw (non-std)\n");
        }
    }
    for (auto& p : done) {
        // A callback that throws (e.g. a json parse on an unexpected payload shape) must
        // not escape into the GL loop — uncaught it would std::terminate the whole app.
        try {
            if (p->done) p->done(p->res);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "[async_rpc] callback for '%s' threw: %s\n",
                         p->method.c_str(), e.what());
        } catch (...) {
            std::fprintf(stderr, "[async_rpc] callback for '%s' threw (non-std)\n",
                         p->method.c_str());
        }
        pending_.fetch_sub(1);
        if (p->blocksInteraction) interactionPending_.fetch_sub(1);
    }
}

void AsyncRpc::shutdown() {
    {
        std::lock_guard<std::mutex> lk(mtx_);
        quit_ = true;
        queue_.clear();   // queued calls never run
        bgQueue_.clear();
        doneQ_.clear();   // undrained callbacks never run
        eventQ_.clear();  // undelivered events never run
    }
    cv_.notify_all();
    // Unblock a round-trip the dispatch threads may be parked in (readLine poll loop).
    worker_.abortPending();
    if (thread_.joinable()) thread_.join();
    if (bgThread_.joinable()) bgThread_.join();
    pending_.store(0);
    interactionPending_.store(0);
}

}  // namespace core
