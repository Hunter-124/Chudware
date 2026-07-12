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
// Env knobs (all optional; shared naming with benchmarks/simsuite/resguard.py):
//   AICAD_GOVERNOR=0     disable every check (admit everything, no clamps)
//   AICAD_MEM_FRAC       fraction of total RAM one study may claim   (default 0.5)
//   AICAD_MEM_CAP_GB     absolute ceiling applied on top of the fraction
//   AICAD_HEADROOM_GB    RAM always left to the OS/GUI               (default 4)
//   AI_CAD_SIM_THREADS   solver thread cap (also set by the Python resguard)
#pragma once

#include <cstdint>
#include <string>

namespace aicad::governor {

struct HostMem {
    uint64_t total = 0;  // physical RAM, bytes (0 = unknown platform)
    uint64_t avail = 0;  // currently available, bytes (0 = unknown)
};

// Cross-platform host memory query (Linux /proc/meminfo, Windows
// GlobalMemoryStatusEx, macOS sysctl+host_statistics; zeros when unknowable).
HostMem host_mem();

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

// Solver thread budget: AI_CAD_SIM_THREADS if set, else 75% of logical cores,
// always >= 1.
int solver_threads();

}  // namespace aicad::governor
