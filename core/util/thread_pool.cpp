// thread_pool.cpp — see thread_pool.h.
#include "util/thread_pool.h"

#include "util/resource_governor.h"

#include <algorithm>
#include <atomic>
#include <exception>
#include <memory>
#include <utility>

namespace chudware::pool {

namespace {

// Depth of pool-worker execution on this thread. >0 means nested submit must
// run inline (the workers are already counted against max_workers_).
thread_local int tls_worker_depth = 0;

// Which pool (if any) this thread belongs to — nested check is per-pool.
thread_local ThreadPool* tls_pool = nullptr;

}  // namespace

ThreadPool::ThreadPool(int max_workers) {
    if (max_workers <= 0) max_workers = chudware::governor::solver_threads();
    max_workers_ = std::max(1, max_workers);
    start_workers(max_workers_);
}

ThreadPool::~ThreadPool() { stop_workers(); }

int ThreadPool::max_workers() const {
    std::lock_guard<std::mutex> lk(mu_);
    return max_workers_;
}

bool ThreadPool::in_worker() const { return tls_pool == this && tls_worker_depth > 0; }

void ThreadPool::start_workers(int n) {
    // Caller holds no assumption about mu_; only called from ctor / set_max with
    // workers_ empty or after stop.
    for (int i = 0; i < n; ++i) {
        workers_.emplace_back([this, i] { worker_loop(i); });
    }
}

void ThreadPool::stop_workers() {
    {
        std::lock_guard<std::mutex> lk(mu_);
        stopping_ = true;
    }
    cv_job_.notify_all();
    for (auto& t : workers_) {
        if (t.joinable()) t.join();
    }
    workers_.clear();
    {
        std::lock_guard<std::mutex> lk(mu_);
        stopping_ = false;
        // Drop any leftover jobs (destructor / resize); they will not run.
        std::queue<std::function<void()>> empty;
        jobs_.swap(empty);
        active_ = 0;
    }
}

void ThreadPool::set_max_workers(int n) {
    n = std::max(1, n);
    // Fast path: same size.
    {
        std::lock_guard<std::mutex> lk(mu_);
        if (n == max_workers_ && !workers_.empty()) return;
    }
    // Wait for idle, then rebuild. Nested set_max from a worker is unsupported
    // (would deadlock on join); refuse by no-op if called in-worker.
    if (in_worker()) return;
    wait();
    stop_workers();
    {
        std::lock_guard<std::mutex> lk(mu_);
        max_workers_ = n;
    }
    start_workers(n);
}

void ThreadPool::worker_loop(int /*id*/) {
    tls_pool = this;
    for (;;) {
        std::function<void()> job;
        {
            std::unique_lock<std::mutex> lk(mu_);
            cv_job_.wait(lk, [this] { return stopping_ || !jobs_.empty(); });
            if (stopping_ && jobs_.empty()) return;
            job = std::move(jobs_.front());
            jobs_.pop();
            ++active_;
        }
        tls_worker_depth++;
        try {
            job();
        } catch (...) {
            // Swallow: parallel_for captures exceptions separately via promise.
            // A bare submit's future will observe the exception if wrapped.
        }
        tls_worker_depth--;
        {
            std::lock_guard<std::mutex> lk(mu_);
            --active_;
            if (jobs_.empty() && active_ == 0) cv_idle_.notify_all();
        }
    }
}

void ThreadPool::parallel_for(int n, const std::function<void(int)>& body) {
    if (n <= 0) return;
    // Serial path: tiny work, single worker, or nested inside this pool.
    if (n == 1 || max_workers() <= 1 || in_worker()) {
        for (int i = 0; i < n; ++i) body(i);
        return;
    }

    // Chunk the range across at most max_workers_ tasks so nested pools cannot
    // observe more concurrent bodies than the budget.
    const int nw = max_workers();
    const int tasks = std::min(n, nw);
    std::atomic<int> next{0};
    std::atomic<int> remaining{tasks};
    std::promise<void> done;
    std::shared_future<void> done_f = done.get_future().share();
    std::mutex exc_mu;
    std::exception_ptr exc;

    auto worker_body = [&]() {
        try {
            for (;;) {
                const int i = next.fetch_add(1, std::memory_order_relaxed);
                if (i >= n) break;
                body(i);
            }
        } catch (...) {
            std::lock_guard<std::mutex> lk(exc_mu);
            if (!exc) exc = std::current_exception();
        }
        if (remaining.fetch_sub(1, std::memory_order_acq_rel) == 1) {
            done.set_value();
        }
    };

    {
        std::lock_guard<std::mutex> lk(mu_);
        for (int t = 0; t < tasks; ++t) jobs_.push(worker_body);
    }
    cv_job_.notify_all();
    done_f.wait();
    if (exc) std::rethrow_exception(exc);
}

std::future<void> ThreadPool::submit(std::function<void()> task) {
    // Nested: run inline so we never enqueue work that only this same worker
    // could drain (deadlock) and never exceed max_workers concurrent bodies.
    if (in_worker() || max_workers() <= 1) {
        std::promise<void> p;
        try {
            task();
            p.set_value();
        } catch (...) {
            p.set_exception(std::current_exception());
        }
        return p.get_future();
    }

    auto shared_task =
        std::make_shared<std::packaged_task<void()>>(std::move(task));
    std::future<void> fut = shared_task->get_future();
    {
        std::lock_guard<std::mutex> lk(mu_);
        jobs_.push([shared_task]() { (*shared_task)(); });
    }
    cv_job_.notify_one();
    return fut;
}

void ThreadPool::wait() {
    if (in_worker()) return;  // would deadlock waiting on ourselves
    std::unique_lock<std::mutex> lk(mu_);
    cv_idle_.wait(lk, [this] { return jobs_.empty() && active_ == 0; });
}

ThreadPool& shared() {
    // Intentionally leaked: process lifetime. Constructed on first use with the
    // current governor CPU allotment.
    static ThreadPool* p = new ThreadPool(0);
    return *p;
}

int reconfigure_shared(uint64_t estimate_bytes, const char* what) {
    const chudware::governor::Plan plan =
        chudware::governor::plan_for(estimate_bytes, what ? what : "pool", 0);
    shared().set_max_workers(plan.workers);
    return plan.workers;
}

}  // namespace chudware::pool
