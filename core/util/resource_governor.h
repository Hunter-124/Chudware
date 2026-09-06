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
// The budget is a MINIMUM of four independent limits, and the binding one wins:
//
//   budget = min( max( floor, min( CHUDWARE_MEM_FRAC x total,
//                                  CHUDWARE_MEM_CAP_GB,
//                                  avail - CHUDWARE_HEADROOM_GB ) ),
//                 cgroup memory.max )
//
// Consequence worth knowing before you turn a knob: on a busy box the third term
// is usually the binding one, so RAISING CHUDWARE_MEM_FRAC ALONE CHANGES NOTHING
// — it only relaxes a term that was not the minimum. When RAM is actually free
// the lever is CHUDWARE_HEADROOM_GB (lower it), and the durable fix is to free
// memory (close the GUI/builds) so `avail` itself rises. The floor
// (min(3 GB, 40% of total)) keeps a briefly-busy box from refusing small jobs.
//
// `cgroup memory.max` is the CGROUP-AWARE term and the only HARD one: /proc/meminfo
// describes the machine, but a process inside a memory-capped cgroup (systemd-run
// -p MemoryMax=, a .service unit, a container) is SIGKILLed by the cgroup OOM
// killer at its own limit while host RAM is still free. The limit is
// min(memory.max, memory.high) over our own cgroup and every ancestor up to the
// root (each honoured only when finite), minus what the OTHER processes sharing
// that cgroup hold — measured as memory.stat `anon` + `shmem` less our own anon
// RSS, NEVER as memory.current, which also counts the reclaimable page cache and
// would collapse the term to nothing after a big STEP/VTU read. Because the limit
// is a kill line it is applied AFTER the small-job floor: no knob may raise a
// budget above it. The sibling subtraction, being an estimate of a moving target,
// does keep the floor — the term is max(claimable, min(floor, limit)) — so one
// sibling's transient GB cannot refuse every study inside a roomy scope, while a
// cgroup limit genuinely below the floor still binds. With no cgroup v2 limit
// anywhere on the path the term is infinite and nothing changes. A refusal on
// this term names the limit AND how much of it the siblings hold.
//
// Env knobs (all optional; shared naming with benchmarks/simsuite/resguard.py):
//   CHUDWARE_GOVERNOR=0     disable every check (admit everything, full threads)
//   CHUDWARE_MEM_FRAC       fraction of total RAM one study may claim   (default 0.5)
//   CHUDWARE_MEM_CAP_GB     absolute ceiling applied on top of the fraction
//   CHUDWARE_HEADROOM_GB    RAM always left to the OS/GUI               (default 4)
//   CHUDWARE_CPU_FRAC       fraction of logical cores for heavy ops     (default 0.75)
//   CHUDWARE_SIM_THREADS   absolute solver thread cap (also set by the Python resguard)
//   CHUDWARE_CGROUP_ROOT    replaces /sys/fs/cgroup AND the /proc/self/cgroup lookup;
//                           the named directory IS the cgroup (leaf == root). Tests
//                           fake memory.max/high/stat with it; also lets you replay a
//                           container's own accounting by hand
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
// doesn't refuse tiny jobs, then clamped to the cgroup term above (a hard kill
// line no knob may cross). UINT64_MAX when the governor is disabled.
uint64_t budget_bytes();

// Admission check for a projected peak. Returns true when the job fits (or the
// governor is off / the platform is unknown). On refusal, `why` gets a
// user-facing message with the numbers and the knobs that would change the
// outcome — callers surface it verbatim as the study error.
bool admit(uint64_t estimate_bytes, const char* what, std::string& why);

// ── Calibrated FEA peak-memory model ────────────────────────────────────────────
// Projected peak working set of ONE direct-solve FEA job (mesh residency +
// triplet assembly + sparse factor fill + stress recovery + result packet), from
// MEASURED peak RSS of the production path — not an analytic guess. The measured
// table, the two solver families it covers and the safety factor are documented
// at the top of resource_governor.cpp; fea_calibration_basis() is the one-line
// summary meant for user-facing messages.
//
// The returned number ALREADY carries the safety factor, so callers compare it to
// budget_bytes() directly. nodes_per_element selects the family (4 = tet4, which
// is locked to Eigen SparseLU by the parity harness; >= 8 = tet10/hex8, which use
// the CHOLMOD/LDLT Cholesky path and cost far less per DOF). dof_per_node is 3
// for structural, 1 for thermal.
uint64_t fea_peak_bytes(uint64_t n_elements, int nodes_per_element, int dof_per_node);

// Inverse of fea_peak_bytes: the largest element count whose projected peak still
// fits `budget` bytes. 0 when even one element does not fit. Meshers use this to
// coarsen a study into the envelope instead of being refused after meshing.
uint64_t fea_element_limit(uint64_t budget, int nodes_per_element, int dof_per_node);

// One-line provenance for the numbers above, e.g. for a study note or refusal.
const char* fea_calibration_basis();

// Admission for a calibrated FEA solve: computes the estimate with
// fea_peak_bytes() and applies the admit() rule, so the refusal names the
// estimate, the calibration basis, the budget AND which of the three budget
// terms is binding (the one knob that would actually change the outcome).
bool admit_fea(uint64_t n_elements, int nodes_per_element, int dof_per_node,
               const char* what, std::string& why);

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
