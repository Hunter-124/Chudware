// Shared CHOLMOD/BLAS worker-thread cap for the sparse sim solvers (P2 / F3).
//
// CHOLMOD's supernodal kernels and the OpenMP-based system BLAS they call will happily
// take every core; a long solve must never starve the GUI/render/IPC threads (see
// fea_static.cpp's apply_solver_thread_cap, whose convention this mirrors). Past ~8
// threads the supernodal DGEMM saturates memory bandwidth on this class of desktop and
// oversubscribes against CHOLMOD's own OpenMP pool, so the cap is min(governed, 8)
// after leaving 2 cores for the GUI when possible.
// CHUDWARE_FEA_THREADS overrides for benchmarking.
//
// F3: the cap is sourced from the resource governor / shared thread pool so nested
// solves share ONE budget instead of each ad-hoc OMP region oversubscribing.
// fea_static keeps its own size-adaptive version (serializes tiny solves); this shared
// helper is the fixed-cap flavour for the modal/thermal paths where the factorization
// is either large enough to matter or cheap enough that the cap is harmless.
#pragma once

#include <algorithm>
#include <cstdlib>
#include <string>
#include <thread>

#include "util/resource_governor.h"
#include "util/thread_pool.h"

#ifdef CHUDWARE_HAVE_OPENMP
#include <omp.h>
#endif

namespace chudware::sim {

// Governed host thread budget for heavy solver kernels: min(pool max, hw-2, 8),
// with CHUDWARE_FEA_THREADS as an absolute override. Always >= 1.
inline int governed_solver_threads() {
    if (const char* e = std::getenv("CHUDWARE_FEA_THREADS")) {
        const int want = std::atoi(e);
        if (want >= 1) return want;
    }
    // Prefer the live shared-pool size (already governor-clamped); fall back to a
    // fresh plan_for so a never-reconfigured process still gets a sane number.
    int pool_n = chudware::pool::shared().max_workers();
    if (pool_n < 1) {
        const auto plan = chudware::governor::plan_for(0, "solver_threads", 0);
        pool_n = plan.workers;
    }
    unsigned hw = std::thread::hardware_concurrency();
    if (hw == 0) hw = 4;
    const int hw_cap = std::max(1, std::min(static_cast<int>(hw) - 2, 8));
    return std::max(1, std::min(pool_n, hw_cap));
}

inline void cap_solver_threads_for_host() {
#if defined(CHUDWARE_HAVE_OPENMP)
    const int want = governed_solver_threads();
    omp_set_num_threads(want);
    // Belt-and-suspenders for BLAS backends that read the env (FlexiBLAS/OpenBLAS).
    const std::string s = std::to_string(want);
#if defined(_WIN32)
    _putenv_s("OMP_NUM_THREADS", s.c_str());
    _putenv_s("OPENBLAS_NUM_THREADS", s.c_str());
#else
    setenv("OMP_NUM_THREADS", s.c_str(), 1);
    setenv("OPENBLAS_NUM_THREADS", s.c_str(), 1);
#endif
#else
    (void)governed_solver_threads();
#endif
}

}  // namespace chudware::sim
