// Shared CHOLMOD/BLAS worker-thread cap for the sparse sim solvers (P2).
//
// CHOLMOD's supernodal kernels and the OpenMP-based system BLAS they call will happily
// take every core; a long solve must never starve the GUI/render/IPC threads (see
// fea_static.cpp's apply_solver_thread_cap, whose convention this mirrors). Past ~8
// threads the supernodal DGEMM saturates memory bandwidth on this class of desktop and
// oversubscribes against CHOLMOD's own OpenMP pool, so the cap is min(hw-2, 8).
// AI_CAD_FEA_THREADS overrides for benchmarking.
//
// fea_static keeps its own size-adaptive version (serializes tiny solves); this shared
// helper is the fixed-cap flavour for the modal/thermal paths where the factorization
// is either large enough to matter or cheap enough that the cap is harmless.
#pragma once

#include <algorithm>
#include <cstdlib>
#include <string>
#include <thread>

#ifdef AICAD_HAVE_OPENMP
#include <omp.h>
#endif

namespace aicad::sim {

inline void cap_solver_threads_for_host() {
#if defined(AICAD_HAVE_OPENMP)
    int want = 0;
    if (const char* e = std::getenv("AI_CAD_FEA_THREADS")) want = std::atoi(e);
    if (want < 1) {
        unsigned hw = std::thread::hardware_concurrency();
        if (hw == 0) hw = 4;
        want = std::max(1, std::min(static_cast<int>(hw) - 2, 8));
    }
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
#endif
}

}  // namespace aicad::sim
