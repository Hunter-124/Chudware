// Host resource governor — keeps heavy sim/mesh jobs inside the machine's memory
// envelope so a too-big study degrades (coarsened mesh, reduced grid) or fails with
// a clear message instead of OOM-crashing the app on low-end machines.
//
// Every budget is a FRACTION of the host, never an absolute number, so the same
// binary behaves sanely on an 8 GB laptop, a 32 GB desktop, or a big CI box.
// Budgets are recomputed on every call (a /proc read costs nothing next to a
// solve), so they also track what is free RIGHT NOW — concurrent studies and
// other apps shrink each other's budgets automatically.
//
// Dynamic plan (F3): plan_for() grades a heavy op to a worker count + memory
// reservation that FILLS available CPU/RAM without crossing the OOM headroom —
// no self-throttle when resources are free, no OOM when they aren't. The shared
// thread pool (util/thread_pool.h) caps total workers to this budget so nested
// solves never oversubscribe.
//
// Env knobs (all optional; shared naming with benchmarks/simsuite/resguard.py):
//   CHUDWARE_GOVERNOR=0     disable every check (admit everything, full threads)
//   CHUDWARE_MEM_FRAC       fraction of total RAM one study may claim   (default 0.5)
//   CHUDWARE_MEM_CAP_GB     absolute ceiling applied on top of the fraction
//   CHUDWARE_HEADROOM_GB    RAM always left to the OS/GUI               (default 4)
//   CHUDWARE_CPU_FRAC       fraction of logical cores for heavy ops     (default 0.75)
//   CHUDWARE_SIM_THREADS   absolute solver thread cap (also set by the Python resguard)
#pragma once

#include <cstdint>
#include <string>

namespace chudware::governor {

struct HostMem {
    uint64_t total = 0;  // physical RAM, bytes (0 = unknown platform)
    uint64_t avail = 0;  // currently available, bytes (0 = unknown)
};

// Cross-platform host memory query (Linux /proc/meminfo, Windows
// GlobalMemoryStatusEx, macOS sysctl+host_statistics; zeros when unknowable).
HostMem host_mem();

// Live process resident set size in bytes (0 when unknowable). Used by plan_for
// so the grade accounts for what this process already holds, not just host free.
uint64_t process_rss_bytes();

// The byte budget one study may claim right now: min(frac*total, cap,
// avail-headroom), floored at min(3 GB, 40% of total) so a briefly-busy box
// doesn't refuse tiny jobs. UINT64_MAX when the governor is disabled.
uint64_t budget_bytes();

// Admission check for a projected peak. Returns true when the job fits (or the
// governor is off / the platform is unknown). On refusal, `why` gets a
// user-facing message with the numbers and the knobs that would change the
// outcome — callers surface it verbatim as the study error.
bool admit(uint64_t estimate_bytes, const char* what, std::string& why);

// Emergency signal for long-running loops: available RAM has dropped under
// half the OS headroom. Callers should stop gracefully at their next
// checkpoint. Always false when the governor is disabled.
bool low_memory_now();

// Solver thread budget: CHUDWARE_SIM_THREADS if set, else CHUDWARE_CPU_FRAC of
// logical cores (default 0.75), always >= 1. Does NOT apply memory pressure —
// prefer plan_for() when the op has a known byte estimate.
int solver_threads();

// Dynamic grade for a heavy op: worker count + memory reservation that fills
// free CPU/RAM without crossing the OOM headroom.
//
//   estimate_bytes  projected peak working set for the op (0 = unknown / CPU only)
//   what            label for the report string (may be null)
//   want_workers    caller preference; 0 = use solver_threads() allotment
//
// workers: under ample memory/CPU equals the allotted cores; under a tight
// budget scales down toward 1. reserve_bytes is min(estimate, budget) and never
// exceeds the headroom-aware free envelope. admitted is false when estimate
// exceeds budget (same rule as admit()); callers that already admit() may ignore
// it. report is a single human-readable line with every decision input.
struct Plan {
    int workers = 1;
    uint64_t reserve_bytes = 0;
    uint64_t budget = 0;
    uint64_t rss = 0;
    uint64_t avail = 0;
    uint64_t total = 0;
    bool admitted = true;
    std::string report;
};

Plan plan_for(uint64_t estimate_bytes, const char* what = nullptr, int want_workers = 0);

// Most recent plan_for()/admit() decision line (empty until the first call).
// Thread-safe snapshot of the last report string.
std::string last_report();

}  // namespace chudware::governor
