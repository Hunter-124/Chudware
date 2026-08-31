// AsyncRpc (C39) implementation — see async_rpc.h.
#include "async_rpc.h"

#include <cstdio>

#include "../worker_client.h"

namespace core {

void AsyncRpc::call(std::string method, nlohmann::json params, Done done, int timeoutMs) {
    call(std::move(method), std::move(params), std::move(done), Event{}, timeoutMs);
}

void AsyncRpc::call(std::string method, nlohmann::json params, Done done, Event onEvent,
                    int timeoutMs) {
    auto p = std::make_unique<Pending>();
    p->method = std::move(method);
    // `rpc.call("m", {}, cb)` looks like "no arguments" but nlohmann parses a bare `{}` as
    // NULL, not an empty object — and a null lands on the wire as `"params": null`. hostd's
    // `msg.value("params", json::object())` then returns the stored null (value() only
    // substitutes the default when the KEY is absent), so the first `params.value(...)` in a
    // handler throws type_error.306 and the daemon rejects the request. That silently broke
    // every zero-argument RPC — parts_taxonomy / parts_list_categories / vendor_status /
    // parts_queue_status / vendors_health_* / login_wizard_* — while calls that pass real
    // arguments worked fine. Normalize here so no null params can ever reach a handler.
    p->params = params.is_null() ? nlohmann::json::object() : std::move(params);
    p->timeoutMs = timeoutMs;
    p->done = std::move(done);
    p->onEvent = std::move(onEvent);
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (quit_) return;  // shutting down — drop silently (callbacks must not run after)
        queue_.push_back(std::move(p));
        pending_.fetch_add(1);
        // GL-thread-only by contract, so the lazy start can't race another call().
        if (!threadStarted_) {
            threadStarted_ = true;
            thread_ = std::thread([this]() { threadMain(); });
        }
    }
    cv_.notify_one();
}

void AsyncRpc::threadMain() {
    for (;;) {
        std::unique_ptr<Pending> p;
        {
            std::unique_lock<std::mutex> lk(mtx_);
            cv_.wait(lk, [this]() { return quit_ || !queue_.empty(); });
            if (quit_) return;
            p = std::move(queue_.front());
            queue_.pop_front();
        }
        // The blocking round-trip happens here, off the GL thread. The errorSink (if any)
        // fires on THIS thread inside call() — it must stay thread-safe (it posts a toast,
        // and AppState::toast locks). Streamed event frames are queued under mtx_ and
        // delivered on the GL thread by drain(); they never run here.
        WorkerClient::EventFn ev;
        if (p->onEvent) {
            Pending* raw = p.get();   // alive until drained; events only fire mid-call
            ev = [this, raw](const nlohmann::json& data) {
                std::lock_guard<std::mutex> lk(mtx_);
                if (quit_) return;
                eventQ_.emplace_back(raw->onEvent, data);
            };
        }
        p->res.ok = worker_.call(p->method, p->params, p->res.result, p->res.err, p->timeoutMs,
                                 ev);
        {
            std::lock_guard<std::mutex> lk(mtx_);
            if (quit_) return;  // result no longer wanted; pending_ is moot past shutdown
            doneQ_.push_back(std::move(p));
        }
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
    }
}

void AsyncRpc::shutdown() {
    {
        std::lock_guard<std::mutex> lk(mtx_);
        quit_ = true;
        queue_.clear();   // queued calls never run
        doneQ_.clear();   // undrained callbacks never run
        eventQ_.clear();  // undelivered events never run
    }
    cv_.notify_one();
    // Unblock a round-trip the dispatch thread may be parked in (readLine poll loop).
    worker_.abortPending();
    if (thread_.joinable()) thread_.join();
    pending_.store(0);
}

}  // namespace core
