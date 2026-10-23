// resource_governor.cpp — see resource_governor.h.
#include "util/resource_governor.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <mutex>
#include <thread>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <psapi.h>
#pragma comment(lib, "psapi.lib")
#elif defined(__APPLE__)
#include <mach/mach.h>
#include <sys/sysctl.h>
#else
#include <unistd.h>
#endif

namespace chudware::governor {

namespace {

constexpr uint64_t kGB = 1024ull * 1024ull * 1024ull;
thread_local HostMemoryProbe host_memory_probe = nullptr;

double env_f(const char* name, double fallback) {
    const char* e = std::getenv(name);
    if (!e || !e[0]) return fallback;
    char* end = nullptr;
    const double v = std::strtod(e, &end);
    return (end != e) ? v : fallback;
}

bool disabled() {
    const char* e = std::getenv("CHUDWARE_GOVERNOR");
    return e && e[0] == '0' && e[1] == '\0';
}

double gb(uint64_t b) { return static_cast<double>(b) / static_cast<double>(kGB); }

// Last decision line (admit / plan_for). Mutex so concurrent studies don't race
// the string; the snapshot is observational only.
std::mutex g_report_mu;
std::string g_last_report;

void store_report(std::string s) {
    std::lock_guard<std::mutex> lk(g_report_mu);
    g_last_report = std::move(s);
}

// CPU allotment before memory pressure: CHUDWARE_SIM_THREADS, else frac of hw.
// Pure CPU budget — no GUI reserve here (callers / plan_for apply that).
int cpu_allotment() {
    if (const char* e = std::getenv("CHUDWARE_SIM_THREADS")) {
        const int v = std::atoi(e);
        if (v > 0) return v;
    }
    const unsigned hc = std::thread::hardware_concurrency();
    const unsigned hw = hc ? hc : 4u;
    const double frac = std::clamp(env_f("CHUDWARE_CPU_FRAC", 0.75), 0.05, 1.0);
    return std::max(1, static_cast<int>(static_cast<double>(hw) * frac + 1e-9));
}

// Headroom-aware free envelope in bytes (0 when nothing is safely free).
// Used both as the admit ceiling partner and as the hard reserve cap.
uint64_t free_envelope(const HostMem& m) {
    if (m.avail == 0) return 0;
    const uint64_t headroom =
        static_cast<uint64_t>(std::max(0.0, env_f("CHUDWARE_HEADROOM_GB", 4.0)) *
                              static_cast<double>(kGB));
    return (m.avail > headroom) ? (m.avail - headroom) : 0;
}

// ── cgroup v2 memory cap ────────────────────────────────────────────────────────
// /proc/meminfo describes the MACHINE. A process inside a memory-capped cgroup
// (systemd-run -p MemoryMax=, a .service unit, a container) is SIGKILLed by the
// cgroup OOM killer at its own limit long before the host runs out, so budgeting
// from meminfo alone admits a solve that then dies uncleanly instead of being
// refused with a message. We therefore walk our own cgroup path to the root and
// take the smallest finite limit: memory.max is the hard kill line, memory.high a
// soft throttle a solve must still not live above (it would thrash reclaim), so
// the smaller of the two is used — and each is honoured ONLY when it is finite
// ("max"/absent must never win that comparison).
//
// The limit is shared with the other processes in that cgroup, so what is really
// claimable is limit - (what they hold). What they hold is measured from
// memory.stat `anon` + `shmem`, NEVER from memory.current: memory.current also
// counts the PAGE CACHE and every other file-backed page charged to the cgroup,
// which is reclaimable under pressure and is not what a solve competes for. That
// distinction is not academic — a 10 GB `systemd-run --scope -p MemoryMax=10G`
// holding a long-running mission driver, its python worker and chudware-hostd had
// streamed STEP/VTU through the page cache until memory.current sat within a few
// hundred MB of the limit, and `limit - (memory.current - own_rss)` collapsed the
// whole budget to 0.4 GB. run_fea_static then refused a study the scope had 8+ GB
// of anonymous room for, twice. Our own contribution is likewise our own ANON
// RSS, not total RSS: total RSS counts our mapped binaries and file pages, which
// are charged to `file`, so subtracting it from an anon figure over-credits us.

// One cgroup control file as bytes; UINT64_MAX when absent, unparsable or "max".
uint64_t cgroup_file_bytes(const std::string& path) {
    uint64_t out = UINT64_MAX;
    if (FILE* f = std::fopen(path.c_str(), "r")) {
        char tok[64] = {0};
        if (std::fscanf(f, "%63s", tok) == 1 && std::strcmp(tok, "max") != 0) {
            char* end = nullptr;
            const unsigned long long v = std::strtoull(tok, &end, 10);
            if (end != tok) out = static_cast<uint64_t>(v);
        }
        std::fclose(f);
    }
    return out;
}

// One key out of a flat "<key> <bytes>" file (memory.stat); 0 when absent.
uint64_t cgroup_stat_bytes(const std::string& path, const char* key) {
    uint64_t out = 0;
    if (FILE* f = std::fopen(path.c_str(), "r")) {
        char k[64] = {0};
        unsigned long long v = 0;
        while (std::fscanf(f, "%63s %llu", k, &v) == 2) {
            if (std::strcmp(k, key) == 0) {
                out = static_cast<uint64_t>(v);
                break;
            }
        }
        std::fclose(f);
    }
    return out;
}

// Our own ANONYMOUS resident set in bytes — what this process contributes to the
// cgroup's `anon` counter, and therefore the only honest thing to add back when
// subtracting "what the others hold". Falls back to total RSS when unknowable.
uint64_t process_anon_rss_bytes() {
#if defined(_WIN32) || defined(__APPLE__)
    return process_rss_bytes();
#else
    if (FILE* f = std::fopen("/proc/self/status", "r")) {
        char line[256];
        while (std::fgets(line, sizeof line, f)) {
            unsigned long long kb = 0;
            if (std::sscanf(line, "RssAnon: %llu kB", &kb) == 1) {
                std::fclose(f);
                return kb * 1024ull;
            }
        }
        std::fclose(f);
    }
    return process_rss_bytes();
#endif
}

// Our cgroup's binding memory limit and what the OTHER processes in it hold.
// limit == UINT64_MAX means uncapped (no cgroup v2, no finite limit on the path)
// and the whole term is inert, so behaviour is unchanged off-cgroup.
struct CgroupMem {
    uint64_t limit = UINT64_MAX;
    uint64_t others = 0;
    bool capped() const { return limit != UINT64_MAX; }
    uint64_t claimable() const { return (limit > others) ? (limit - others) : 0; }
};

CgroupMem cgroup_mem() {
#if defined(_WIN32) || defined(__APPLE__)
    return {};
#else
    // CHUDWARE_CGROUP_ROOT replaces /sys/fs/cgroup AND the /proc/self/cgroup
    // lookup: the named directory is then treated as THE cgroup (leaf == root),
    // so the ancestor walk starts and ends there. Tests fake the control files
    // with it; it is also the way to replay a container's own view by hand.
    const char* override_root = std::getenv("CHUDWARE_CGROUP_ROOT");
    std::string root = "/sys/fs/cgroup";
    std::string rel = "/";
    if (override_root && override_root[0]) {
        root = override_root;
    } else {
        rel.clear();
        if (FILE* f = std::fopen("/proc/self/cgroup", "r")) {
            char line[512];
            while (std::fgets(line, sizeof line, f)) {
                // Unified (v2) line only: "0::/user.slice/…"; v1 lines name controllers.
                if (std::strncmp(line, "0::", 3) != 0) continue;
                rel = line + 3;
                while (!rel.empty() && (rel.back() == '\n' || rel.back() == '\r'))
                    rel.pop_back();
                break;
            }
            std::fclose(f);
        }
        if (rel.empty() || rel[0] != '/') return {};
    }
    CgroupMem out;
    uint64_t usage = 0;  // anon + shmem of the cgroup that owns the binding limit
    for (;;) {
        const std::string dir = root + ((rel == "/") ? std::string() : rel);
        uint64_t lim = cgroup_file_bytes(dir + "/memory.max");
        const uint64_t high = cgroup_file_bytes(dir + "/memory.high");
        if (high < lim) lim = high;  // both are UINT64_MAX when unset: stays inert
        if (lim < out.limit) {
            out.limit = lim;
            const std::string stat = dir + "/memory.stat";
            usage = cgroup_stat_bytes(stat, "anon") + cgroup_stat_bytes(stat, "shmem");
        }
        if (rel == "/") break;
        const std::size_t slash = rel.rfind('/');
        rel = (slash == 0) ? std::string("/") : rel.substr(0, slash);
    }
    if (!out.capped()) return {};
    const uint64_t own = process_anon_rss_bytes();
    out.others = (usage > own) ? (usage - own) : 0;
    return out;
#endif
}

// ── Which budget term binds ─────────────────────────────────────────────────────
// budget_bytes() is a MINIMUM of four terms plus a floor. A refusal that does not
// say WHICH term produced the number is useless advice: on a busy box `avail -
// headroom` is almost always the smallest, and raising CHUDWARE_MEM_FRAC then
// changes nothing at all. Both budget_bytes() and the refusal text come from this
// one computation so they can never disagree. The cgroup term is applied LAST,
// after the small-job floor, because it is the only HARD term: crossing it is a
// SIGKILL, and no floor may raise a budget above it.
enum class Term { Frac, Cap, Free, Floor, Cgroup };

struct BudgetCalc {
    uint64_t bytes = 0;
    Term binding = Term::Frac;
    uint64_t cg_limit = UINT64_MAX;  // binding cgroup limit (UINT64_MAX = uncapped)
    uint64_t cg_others = 0;          // anon+shmem held by the rest of that cgroup
};

BudgetCalc compute_budget(const HostMem& m) {
    BudgetCalc out;
    const double frac = std::clamp(env_f("CHUDWARE_MEM_FRAC", 0.5), 0.05, 1.0);
    out.bytes = static_cast<uint64_t>(static_cast<double>(m.total) * frac);
    out.binding = Term::Frac;
    const double cap_gb = env_f("CHUDWARE_MEM_CAP_GB", 0.0);
    if (cap_gb > 0.0) {
        const uint64_t cap = static_cast<uint64_t>(cap_gb * static_cast<double>(kGB));
        if (cap < out.bytes) {
            out.bytes = cap;
            out.binding = Term::Cap;
        }
    }
    if (m.avail > 0) {
        const uint64_t free_now = std::max(kGB, free_envelope(m));
        if (free_now < out.bytes) {
            out.bytes = free_now;
            out.binding = Term::Free;
        }
    }
    // Floor: a briefly-busy box must not refuse small jobs outright.
    const uint64_t floor_b =
        std::min(3 * kGB, static_cast<uint64_t>(static_cast<double>(m.total) * 0.4));
    if (floor_b > out.bytes) {
        out.bytes = floor_b;
        out.binding = Term::Floor;
    }
    // Hard cgroup cap (systemd MemoryMax / container limit): outranks the floor,
    // because crossing it is a SIGKILL and not merely a slow box.
    //
    // The SUBTRACTION of what our cgroup siblings hold, however, is an estimate of
    // a moving target, and it gets the same small-job floor the host terms get: a
    // sibling's transient GB may not turn a 10 GB scope into a budget that refuses
    // every real study. So the term is max(claimable, min(floor, limit)) — the
    // floor still applies, but it can never lift the budget above the kill line
    // itself, and a cgroup limit BELOW the floor (a deliberately tiny container)
    // binds at that limit with no rescue.
    const CgroupMem cg = cgroup_mem();
    out.cg_limit = cg.limit;
    out.cg_others = cg.others;
    if (cg.capped()) {
        const uint64_t cg_bytes = std::max(cg.claimable(), std::min(floor_b, cg.limit));
        if (cg_bytes < out.bytes) {
            out.bytes = cg_bytes;
            out.binding = Term::Cgroup;
        }
    }
    return out;
}

// Which limit produced the budget, and the ONLY knob that moves it. Every branch
// names the knob vocabulary (CHUDWARE_MEM_FRAC / CHUDWARE_MEM_CAP_GB /
// CHUDWARE_HEADROOM_GB) so the message is actionable whichever term won —
// including saying which knobs are useless right now. The cgroup branch also
// names how much of the limit the OTHER processes in the cgroup hold: without
// that number a 10 GB scope reporting a 0.4 GB budget looks like a governor bug
// instead of what it is — the driver and the daemon sharing the same cap.
std::string binding_text(const BudgetCalc& b) {
    switch (b.binding) {
        case Term::Cap:
            return "binding limit: CHUDWARE_MEM_CAP_GB — raise it; CHUDWARE_MEM_FRAC "
                   "and CHUDWARE_HEADROOM_GB cannot help while the cap binds";
        case Term::Free:
            return "binding limit: available RAM minus CHUDWARE_HEADROOM_GB — lower "
                   "CHUDWARE_HEADROOM_GB or free RAM (close the GUI/builds); raising "
                   "CHUDWARE_MEM_FRAC or CHUDWARE_MEM_CAP_GB changes nothing";
        case Term::Floor:
            return "binding limit: the small-job floor min(3 GB, 40% of RAM) — this "
                   "machine is too small for the study; CHUDWARE_MEM_FRAC and "
                   "CHUDWARE_MEM_CAP_GB cannot raise a floor";
        case Term::Cgroup: {
            char buf[512];
            std::snprintf(
                buf, sizeof buf,
                "binding limit: cgroup memory limit %.1f GB — this process runs inside "
                "a memory-capped cgroup (systemd MemoryMax / container limit), and the "
                "other processes sharing it hold %.1f GB of that limit (anonymous + "
                "shmem), leaving %.1f GB for this solve; give the solve its own scope or "
                "raise the cgroup limit; CHUDWARE_MEM_FRAC, CHUDWARE_MEM_CAP_GB and "
                "CHUDWARE_HEADROOM_GB cannot raise it",
                gb(b.cg_limit), gb(b.cg_others), gb(b.bytes));
            return buf;
        }
        case Term::Frac:
        default:
            return "binding limit: CHUDWARE_MEM_FRAC x total RAM — raise "
                   "CHUDWARE_MEM_FRAC (CHUDWARE_HEADROOM_GB is not the constraint "
                   "right now)";
    }
}

// ── Measured FEA peak-memory calibration ────────────────────────────────────────
// MEASURED peak RSS (VmHWM) of the production static-solve path — mesh residency,
// assembly, sparse factorization, stress recovery and result packet — one process
// per point, 100 mm solid box meshed by polymesh at an EXPLICIT target size,
// CHUDWARE_SIM_THREADS=2, this repo's core/build-occt-linux (CHOLMOD ON),
// re-measured 2026-09-05 AFTER the solver rewrite. Peaks are net of the ~45 MB
// process baseline, because the governor budgets the JOB, not the process.
//
// What changed under these numbers (core/sim/fea_static.cpp):
//   * assembly no longer builds a triplet list — (3*npe)^2 * 16 B per element, i.e.
//     14.4 KB for one tet10, held all the way through the factorization — it scatters
//     straight into a sparsity pattern built once (4.1 KB/element measured on tet10),
//   * BOTH families now factor through the same SPD ladder, CHOLMOD supernodal LLT
//     first. tet4 is no longer pinned to Eigen SparseLU (the Python parity oracle
//     that pinned it no longer exists in the tree), and SparseLU's fill on a bulky
//     3D solid was the single largest term in the old tet4 curve.
//
// Same element counts, before -> after, net GB:
//   tet4    15264 el /   9141 dof : 0.091 -> 0.047
//   tet4    26064 el /  15459 dof : 0.168 -> 0.071
//   tet4    38832 el /  22881 dof : 0.269 -> 0.093
//   tet4   ~100k  el /  59049 dof : 1.414 -> 0.256   (101424 el after, 100080 before)
//   tet4   ~149k  el /  85965 dof :   —   -> 0.547
//   tet4   ~206k  el / 120753 dof : 6.148 -> 0.832   (212592 el after, 202992 before)
//   tet10   15264 el /  66591 dof :   —   -> 0.357
//   tet10   26064 el / 113211 dof :   —   -> 0.620
//   tet10   38832 el / 168087 dof : 1.329 -> 0.979
//   tet10   52176 el / 225555 dof : 1.893 -> 1.350
//   tet10   83184 el / 358635 dof :   —   -> 2.527
//   tet10  101424 el / 436551 dof : 4.425 -> 3.284   (102576 el before)
//   tet10  149424 el / 639327 dof : 9.576 -> 5.817   (148848 el before)
//
// So tet4 is 5.4x-7.4x cheaper than the numbers this table used to carry, and tet10
// 1.35x-1.65x cheaper. The tet4 gain is mostly the factorization swap; the tet10 gain
// is mostly the triplet removal, since tet10 already factored through CHOLMOD.
//
// The model is the measured envelope itself: log-log interpolation between table
// points, assembly-linear below the first point, and above the last point the
// STEEPEST slope the tail supports — max(last segment, last two spans), see table_gb.
// That resolves a trap: one dominated point is excluded so the table stays monotone
// (149424 tet4 elements measured 0.422 GB while 148848 elements measured 0.547 GB —
// the SAME element count can differ on mesh topology alone, so the model tracks the
// WORSE mesh), but keeping the worse node FLATTENS the final tet4 segment to p=1.18
// where the broader measured trend is p=1.47 (83184->212592). Extrapolating on the
// flattened segment would under-budget every mesh past the table. The rule in
// table_gb spans the last TWO segments, so the effective slopes above the table are
// tet4 p=1.59 (101424->212592, steeper than both its final segment and the broader
// trend, so conservative) and tet10 p=1.48 (its final segment, 101424->149424,
// already steeper than its two-segment span at 1.42). A x1.25 SAFETY FACTOR is then
// applied inside fea_peak_bytes(), covering machine-to-machine and mesh-to-mesh
// spread.
// Re-measure (do not scale) whenever the default factorization or the assembly shape
// changes: one process per point running fea_run_static on a box STEP with an explicit
// target_size_mm, CHUDWARE_GOVERNOR=0, reading VmHWM from /proc/self/status.
// And do NOT hand-derive the slopes quoted above from the table by eye — that number
// has diverged from the code three times, because table_gb picks its span by INDEX
// (t[N-3]/t[N-2]/t[N-1]) and the winning span moves when a point is added or a
// dominated one excluded. Read the effective slope out of the built model instead:
// evaluate fea_peak_bytes at the last table element count and at twice that, then
// p = log2(peak_2n / peak_n).
struct CalPoint {
    double elems;
    double gb;
};

// These tables are keyed to the SOLVER PATH, not to the element type: they are only
// valid while both families assemble into a fixed pattern and factor through the
// CHOLMOD -> SimplicialLDLT -> SparseLU ladder in fea_static.cpp. Forcing a different
// factorization with CHUDWARE_FEA_SOLVER makes them wrong — SimplicialLDLT(AMD) alone
// measured 1.52 GB where the ladder needs 0.99 GB on 20250 tet10 elements.
// tet4 — fixed-pattern assembly + SPD ladder.
constexpr CalPoint kTet4[] = {{15264.0, 0.047},  {26064.0, 0.071},   {38832.0, 0.093},
                              {52176.0, 0.127},  {83184.0, 0.210},   {101424.0, 0.256},
                              {148848.0, 0.547}, {212592.0, 0.832}};
// tet10 / hex8 — fixed-pattern assembly + SPD ladder.
constexpr CalPoint kTet10[] = {{15264.0, 0.357},  {26064.0, 0.620},   {38832.0, 0.979},
                               {52176.0, 1.350},  {83184.0, 2.527},   {101424.0, 3.284},
                               {149424.0, 5.817}};
constexpr double kFeaSafety = 1.25;

double seg_gb(const CalPoint& a, const CalPoint& b, double n) {
    const double p = std::log(b.gb / a.gb) / std::log(b.elems / a.elems);
    return a.gb * std::pow(n / a.elems, p);
}

template <std::size_t N>
double table_gb(const CalPoint (&t)[N], double n) {
    if (!(n > 0.0)) return 0.0;
    // Below the table the cost is assembly-dominated, i.e. linear per element.
    if (n <= t[0].elems) return t[0].gb * n / t[0].elems;
    for (std::size_t i = 1; i < N; ++i)
        if (n <= t[i].elems) return seg_gb(t[i - 1], t[i], n);
    // ABOVE the table, extrapolate on the STEEPEST slope the tail supports: the final
    // segment, or the span covering the final TWO segments when that is steeper. A
    // single raised node flattens the final segment without any measured slowdown in
    // fill growth — the excluded dominated tet4 point does exactly that (keeping
    // 148848 el at 0.547 GB over 149424 el at 0.422 GB is right for interpolation,
    // but it drops the final segment to p=1.18 against a p=1.47 measured trend). For
    // tet4 the two-segment span (101424->212592) is p=1.59 and wins; for tet10 the
    // final segment (p=1.48) already beats its two-segment span (p=1.42). Taking the
    // steeper keeps the extrapolation on the measured trend rather than on an
    // outlier-exclusion artifact, and never under-budgets relative to either.
    const double tail = seg_gb(t[N - 2], t[N - 1], n);
    if (N < 3) return tail;
    return std::max(tail, seg_gb(t[N - 3], t[N - 1], n));
}

double fea_peak_gb(double n_elements, int nodes_per_element, int dof_per_node) {
    const int npe = std::max(1, nodes_per_element);
    const int dpn = std::max(1, dof_per_node);
    const bool quadratic = npe >= 8;
    // Within a family, cost scales with the element band (dof_per_node*npe)^2 —
    // exact for assembly, a documented approximation for factor fill. It carries
    // hex8 (band 24) off the tet10 curve and thermal (1 dof/node) off both.
    const double ratio =
        (static_cast<double>(dpn) * static_cast<double>(npe)) / (3.0 * (quadratic ? 10.0 : 4.0));
    const double raw = quadratic ? table_gb(kTet10, n_elements) : table_gb(kTet4, n_elements);
    return raw * ratio * ratio * kFeaSafety;
}

}  // namespace

void MeshCountModel::observe(double measured_h, double measured_elems) {
    if (!(measured_h > 0.0) || !(measured_elems > 0.0) ||
        !std::isfinite(measured_h) || !std::isfinite(measured_elems)) return;
    if (h > 0.0 && elems > 0.0 &&
        std::abs(std::log(h / measured_h)) > 1e-6)
        exponent = std::clamp(std::log(measured_elems / elems) /
                                  std::log(h / measured_h), -3.0, 3.0);
    h = measured_h;
    elems = measured_elems;
}

double MeshCountModel::predict(double target_h) const {
    if (!(h > 0.0) || !(target_h > 0.0) ||
        target_h < h * 0.5 || target_h > h * 2.0)
        return std::numeric_limits<double>::quiet_NaN();
    return elems * std::pow(h / target_h, exponent);
}

HostMemoryProbe set_host_memory_probe(HostMemoryProbe probe) {
    const auto previous = host_memory_probe;
    host_memory_probe = probe;
    return previous;
}

HostMem host_mem() {
    if (host_memory_probe) return host_memory_probe();
    HostMem m;
#if defined(_WIN32)
    MEMORYSTATUSEX st;
    st.dwLength = sizeof st;
    if (GlobalMemoryStatusEx(&st)) {
        m.total = st.ullTotalPhys;
        m.avail = st.ullAvailPhys;
    }
#elif defined(__APPLE__)
    uint64_t total = 0;
    size_t len = sizeof total;
    if (sysctlbyname("hw.memsize", &total, &len, nullptr, 0) == 0) m.total = total;
    vm_size_t page = 0;
    vm_statistics64_data_t vm;
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    if (host_page_size(mach_host_self(), &page) == KERN_SUCCESS &&
        host_statistics64(mach_host_self(), HOST_VM_INFO64,
                          reinterpret_cast<host_info64_t>(&vm), &count) == KERN_SUCCESS) {
        // free + inactive is the closest analogue of Linux MemAvailable.
        m.avail = static_cast<uint64_t>(vm.free_count + vm.inactive_count) * page;
    }
#else  // Linux and friends with procfs
    if (FILE* f = std::fopen("/proc/meminfo", "r")) {
        char line[128];
        while (std::fgets(line, sizeof line, f)) {
            unsigned long long kb = 0;
            if (std::sscanf(line, "MemTotal: %llu kB", &kb) == 1)
                m.total = kb * 1024ull;
            else if (std::sscanf(line, "MemAvailable: %llu kB", &kb) == 1)
                m.avail = kb * 1024ull;
            if (m.total && m.avail) break;
        }
        std::fclose(f);
    }
#endif
    return m;
}

uint64_t process_rss_bytes() {
#if defined(_WIN32)
    PROCESS_MEMORY_COUNTERS pmc;
    if (GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof pmc))
        return static_cast<uint64_t>(pmc.WorkingSetSize);
    return 0;
#elif defined(__APPLE__)
    mach_task_basic_info_data_t info;
    mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
    if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO,
                  reinterpret_cast<task_info_t>(&info), &count) == KERN_SUCCESS)
        return static_cast<uint64_t>(info.resident_size);
    return 0;
#else
    // /proc/self/statm: size resident shared text lib data dt (pages).
    if (FILE* f = std::fopen("/proc/self/statm", "r")) {
        unsigned long size = 0, resident = 0;
        if (std::fscanf(f, "%lu %lu", &size, &resident) == 2) {
            std::fclose(f);
            const long page = sysconf(_SC_PAGESIZE);
            if (page > 0) return static_cast<uint64_t>(resident) * static_cast<uint64_t>(page);
            return 0;
        }
        std::fclose(f);
    }
    return 0;
#endif
}

uint64_t budget_bytes() {
    if (disabled()) return UINT64_MAX;
    const HostMem m = host_mem();
    if (m.total == 0) return UINT64_MAX;  // unknowable platform: never block work
    return compute_budget(m).bytes;
}

bool admit(uint64_t estimate_bytes, const char* what, std::string& why) {
    const uint64_t budget = budget_bytes();
    if (estimate_bytes <= budget) {
        char okbuf[256];
        std::snprintf(okbuf, sizeof okbuf,
                      "governor: admit ok what=%s estimate=%.2fGB budget=%.2fGB",
                      what && what[0] ? what : "study", gb(estimate_bytes), gb(budget));
        store_report(okbuf);
        return true;
    }
    const HostMem m = host_mem();
    char buf[1024];
    std::snprintf(buf, sizeof buf,
                  "%s needs an estimated %.1f GB but this machine's budget is %.1f GB "
                  "(%.0f GB RAM, %.1f GB available; %s) — use a coarser mesh / fewer "
                  "cells / first-order elements, or change the binding limit",
                  what && what[0] ? what : "this study", gb(estimate_bytes), gb(budget),
                  gb(m.total), gb(m.avail), binding_text(compute_budget(m)).c_str());
    why = buf;
    store_report(buf);
    return false;
}

uint64_t fea_peak_bytes(uint64_t n_elements, int nodes_per_element, int dof_per_node) {
    const double g =
        fea_peak_gb(static_cast<double>(n_elements), nodes_per_element, dof_per_node);
    if (!(g > 0.0)) return 0;
    const double bytes = g * static_cast<double>(kGB);
    constexpr double kMax = 1.8e19;  // just inside uint64_t
    return bytes >= kMax ? UINT64_MAX : static_cast<uint64_t>(bytes);
}

uint64_t fea_element_limit(uint64_t budget, int nodes_per_element, int dof_per_node) {
    if (budget == UINT64_MAX || budget == 0) return 0;  // governor off / no budget: no limit
    if (fea_peak_bytes(1, nodes_per_element, dof_per_node) > budget) return 0;
    // Bisection on the forward model, so the limit can never disagree with the
    // estimate a later admit() will compute. 4.3e9 elements is far past any mesh.
    uint64_t lo = 1, hi = 1ull << 32;
    while (lo + 1 < hi) {
        const uint64_t mid = lo + (hi - lo) / 2;
        if (fea_peak_bytes(mid, nodes_per_element, dof_per_node) <= budget)
            lo = mid;
        else
            hi = mid;
    }
    return lo;
}

const char* fea_calibration_basis() {
    return "measured peak RSS after the 2026-09-05 solver rewrite, 100 mm solid box, "
           "polymesh graded tets: tet4 15k-213k el, tet10 15k-149k el, both on the "
           "CHOLMOD/LDLT SPD ladder with fixed-pattern assembly, x1.25 safety";
}

bool admit_fea(uint64_t n_elements, int nodes_per_element, int dof_per_node,
               const char* what, std::string& why) {
    const uint64_t est = fea_peak_bytes(n_elements, nodes_per_element, dof_per_node);
    if (admit(est, what, why)) return true;
    // std::string, not a fixed buffer: the refusal already carries the binding-term
    // explanation (the cgroup branch alone is ~400 chars) and must not be truncated.
    why += " [estimate basis: ";
    why += fea_calibration_basis();
    why += "]";
    store_report(why);
    return false;
}

bool low_memory_now() {
    if (disabled()) return false;
    const HostMem m = host_mem();
    if (m.avail == 0) return false;  // unknowable: never trip
    const uint64_t headroom =
        static_cast<uint64_t>(std::max(0.0, env_f("CHUDWARE_HEADROOM_GB", 4.0)) *
                              static_cast<double>(kGB));
    return m.avail < std::max(kGB, headroom / 2);
}

int solver_threads() {
    return cpu_allotment();
}

Plan plan_for(uint64_t estimate_bytes, const char* what, int want_workers) {
    Plan p;
    const HostMem m = host_mem();
    p.total = m.total;
    p.avail = m.avail;
    p.rss = process_rss_bytes();
    p.budget = budget_bytes();

    const int allot = (want_workers > 0) ? want_workers : cpu_allotment();
    const char* label = (what && what[0]) ? what : "op";

    // Disabled / unknown host: fill every allotted core, reserve the estimate
    // verbatim (no envelope to clamp against). Never self-throttle.
    if (disabled() || p.budget == UINT64_MAX) {
        p.workers = std::max(1, allot);
        p.reserve_bytes = estimate_bytes;
        p.admitted = true;
        char buf[384];
        std::snprintf(buf, sizeof buf,
                      "governor: plan what=%s workers=%d reserve=%.2fGB budget=inf "
                      "rss=%.2fGB avail=%.2fGB total=%.2fGB pressure=0 admitted=1 "
                      "(governor off or host unknown) knobs=CHUDWARE_GOVERNOR,CHUDWARE_SIM_THREADS,"
                      "CHUDWARE_CPU_FRAC",
                      label, p.workers, gb(p.reserve_bytes), gb(p.rss), gb(p.avail),
                      gb(p.total));
        p.report = buf;
        store_report(p.report);
        return p;
    }

    // Free envelope after OS/GUI headroom — hard cap on what we may reserve.
    const uint64_t envelope = free_envelope(m);
    // Effective free for this process: budget already folds frac/cap/envelope;
    // also subtract live RSS so a fat process does not double-book RAM.
    uint64_t effective = p.budget;
    if (p.rss > 0 && effective > p.rss) effective -= p.rss / 4;  // partial credit: RSS overlaps working set

    // Reservation: never past the budget, never past the headroom envelope when known.
    uint64_t reserve = estimate_bytes;
    if (reserve > p.budget) reserve = p.budget;
    if (envelope > 0 && reserve > envelope) reserve = envelope;
    p.reserve_bytes = reserve;

    p.admitted = (estimate_bytes <= p.budget);

    // Worker grade: fill allotted cores when pressure is low; scale toward 1 as
    // the estimate approaches / exceeds the effective free envelope. Pressure 0
    // (no estimate, or tiny job) => full allotment — no artificial throttle.
    int workers = std::max(1, allot);
    double pressure = 0.0;
    if (estimate_bytes > 0 && effective > 0) {
        pressure = static_cast<double>(estimate_bytes) / static_cast<double>(effective);
        if (pressure <= 0.25) {
            workers = std::max(1, allot);  // ample: use every allotted core
        } else if (pressure >= 1.0) {
            workers = 1;  // at or over budget: serialise to limit peak
        } else {
            // Linear taper from full allotment at 0.25 to 1 at 1.0.
            const double t = (pressure - 0.25) / 0.75;  // 0..1
            const double scaled = static_cast<double>(allot) * (1.0 - t) + 1.0 * t;
            workers = std::max(1, static_cast<int>(scaled + 0.5));
            workers = std::min(workers, allot);
        }
    }
    p.workers = workers;

    char buf[448];
    std::snprintf(buf, sizeof buf,
                  "governor: plan what=%s workers=%d/%d reserve=%.2fGB budget=%.2fGB "
                  "rss=%.2fGB avail=%.2fGB total=%.2fGB pressure=%.2f admitted=%d "
                  "knobs=CHUDWARE_MEM_FRAC,CHUDWARE_MEM_CAP_GB,CHUDWARE_HEADROOM_GB,"
                  "CHUDWARE_CPU_FRAC,CHUDWARE_SIM_THREADS",
                  label, p.workers, allot, gb(p.reserve_bytes), gb(p.budget), gb(p.rss),
                  gb(p.avail), gb(p.total), pressure, p.admitted ? 1 : 0);
    p.report = buf;
    store_report(p.report);
    return p;
}

std::string last_report() {
    std::lock_guard<std::mutex> lk(g_report_mu);
    return g_last_report;
}

}  // namespace chudware::governor
