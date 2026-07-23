// Bounded work-queue thread pool shared by heavy sim / mesh / route jobs.
//
// Max workers come from the resource governor (util/resource_governor.h) so
// nested solves share ONE CPU budget instead of each spawning ad-hoc OpenMP
// regions that oversubscribe. Nested parallel_for / submit from a pool worker
// runs inline on the caller — never grows past max_workers().
//
// Env: CHUDWARE_SIM_THREADS / CHUDWARE_CPU_FRAC (via governor) set the default size;
// reconfigure() re-reads the governor after a plan_for() decision.
#pragma once

#include <condition_variable>
#include <functional>
#include <future>
#include <mutex>
#include <queue>
#include <thread>
#include <vector>

namespace chudware::pool {

class ThreadPool {
public:
    // max_workers <= 0 -> governor::solver_threads() at construction time.
    explicit ThreadPool(int max_workers = 0);
    ~ThreadPool();

    ThreadPool(const ThreadPool&) = delete;
    ThreadPool& operator=(const ThreadPool&) = delete;

    int max_workers() const;
    // Grow / shrink the worker set. Safe when the pool is idle; may block
    // joining excess workers. Clamped to >= 1.
    void set_max_workers(int n);

    // true when the calling thread is one of this pool's workers (used to
    // detect nested submits and avoid oversubscription / deadlock).
    bool in_worker() const;

    // Run body(i) for i in [0, n). Blocks until every index is done.
    // Nested-safe: from a pool worker, runs serially on the caller.
    void parallel_for(int n, const std::function<void(int)>& body);

    // Enqueue one task. Returns a future; blocks the worker budget the same way
    // as parallel_for. Nested from a worker: runs inline and returns a ready future.
    std::future<void> submit(std::function<void()> task);

    // Block until the queue is empty and no tasks are running.
    void wait();

private:
    void worker_loop(int id);
    void start_workers(int n);
    void stop_workers();

    mutable std::mutex mu_;
    std::condition_variable cv_job_;
    std::condition_variable cv_idle_;
    std::queue<std::function<void()>> jobs_;
    std::vector<std::thread> workers_;
    int max_workers_ = 1;
    int active_ = 0;       // tasks currently executing on workers
    bool stopping_ = false;
};

// Process-wide shared pool. First call constructs with governor::solver_threads().
ThreadPool& shared();

// Resize the shared pool from a governor plan (estimate 0 => CPU-only allotment).
// Returns the workers applied. Observability: governor::last_report() after this.
int reconfigure_shared(uint64_t estimate_bytes = 0, const char* what = nullptr);

}  // namespace chudware::pool
