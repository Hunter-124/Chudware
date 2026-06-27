// AsyncRpc (C39) implementation — see async_rpc.h.
#include "async_rpc.h"

#include "../worker_client.h"

namespace core {

void AsyncRpc::call(std::string method, nlohmann::json params, Done done, int timeoutMs) {
    auto p = std::make_unique<Pending>();
    p->method = std::move(method);
    p->params = std::move(params);
    p->timeoutMs = timeoutMs;
    p->done = std::move(done);
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
        // and AppState::toast locks).
        p->res.ok = worker_.call(p->method, p->params, p->res.result, p->res.err, p->timeoutMs);
        {
            std::lock_guard<std::mutex> lk(mtx_);
            if (quit_) return;  // result no longer wanted; pending_ is moot past shutdown
            doneQ_.push_back(std::move(p));
        }
    }
}

void AsyncRpc::drain() {
    std::vector<std::unique_ptr<Pending>> done;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (doneQ_.empty()) return;
        done.swap(doneQ_);
    }
    for (auto& p : done) {
        if (p->done) p->done(p->res);
        pending_.fetch_sub(1);
    }
}

void AsyncRpc::shutdown() {
    {
        std::lock_guard<std::mutex> lk(mtx_);
        quit_ = true;
        queue_.clear();   // queued calls never run
        doneQ_.clear();   // undrained callbacks never run
    }
    cv_.notify_one();
    // Unblock a round-trip the dispatch thread may be parked in (readLine poll loop).
    worker_.abortPending();
    if (thread_.joinable()) thread_.join();
    pending_.store(0);
}

}  // namespace core
