// resource_governor.cpp — see resource_governor.h.
#include "util/resource_governor.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
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

namespace aicad::governor {

namespace {

constexpr uint64_t kGB = 1024ull * 1024ull * 1024ull;

double env_f(const char* name, double fallback) {
    const char* e = std::getenv(name);
    if (!e || !e[0]) return fallback;
    char* end = nullptr;
    const double v = std::strtod(e, &end);
    return (end != e) ? v : fallback;
}

bool disabled() {
    const char* e = std::getenv("AICAD_GOVERNOR");
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

// CPU allotment before memory pressure: AI_CAD_SIM_THREADS, else frac of hw.
// Pure CPU budget — no GUI reserve here (callers / plan_for apply that).
int cpu_allotment() {
    if (const char* e = std::getenv("AI_CAD_SIM_THREADS")) {
        const int v = std::atoi(e);
        if (v > 0) return v;
    }
    const unsigned hc = std::thread::hardware_concurrency();
    const unsigned hw = hc ? hc : 4u;
    const double frac = std::clamp(env_f("AICAD_CPU_FRAC", 0.75), 0.05, 1.0);
    return std::max(1, static_cast<int>(static_cast<double>(hw) * frac + 1e-9));
}

// Headroom-aware free envelope in bytes (0 when nothing is safely free).
// Used both as the admit ceiling partner and as the hard reserve cap.
uint64_t free_envelope(const HostMem& m) {
    if (m.avail == 0) return 0;
    const uint64_t headroom =
        static_cast<uint64_t>(std::max(0.0, env_f("AICAD_HEADROOM_GB", 4.0)) *
                              static_cast<double>(kGB));
    return (m.avail > headroom) ? (m.avail - headroom) : 0;
}

}  // namespace

HostMem host_mem() {
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
    const double frac = std::clamp(env_f("AICAD_MEM_FRAC", 0.5), 0.05, 1.0);
    uint64_t budget = static_cast<uint64_t>(static_cast<double>(m.total) * frac);
    const double cap_gb = env_f("AICAD_MEM_CAP_GB", 0.0);
    if (cap_gb > 0.0)
        budget = std::min(budget, static_cast<uint64_t>(cap_gb * static_cast<double>(kGB)));
    const uint64_t headroom =
        static_cast<uint64_t>(std::max(0.0, env_f("AICAD_HEADROOM_GB", 4.0)) *
                              static_cast<double>(kGB));
    if (m.avail > 0) {
        const uint64_t now_free = (m.avail > headroom) ? (m.avail - headroom) : 0;
        budget = std::min(budget, std::max(kGB, now_free));
    }
    // Floor: a briefly-busy box must not refuse small jobs outright.
    const uint64_t floor_b =
        std::min(3 * kGB, static_cast<uint64_t>(static_cast<double>(m.total) * 0.4));
    return std::max(budget, floor_b);
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
    char buf[352];
    std::snprintf(buf, sizeof buf,
                  "%s needs an estimated %.1f GB but this machine's budget is %.1f GB "
                  "(%.0f GB RAM, %.1f GB available) — use a coarser mesh / fewer cells / "
                  "first-order elements, or raise AICAD_MEM_FRAC / AICAD_MEM_CAP_GB",
                  what && what[0] ? what : "this study", gb(estimate_bytes), gb(budget),
                  gb(m.total), gb(m.avail));
    why = buf;
    store_report(buf);
    return false;
}

bool low_memory_now() {
    if (disabled()) return false;
    const HostMem m = host_mem();
    if (m.avail == 0) return false;  // unknowable: never trip
    const uint64_t headroom =
        static_cast<uint64_t>(std::max(0.0, env_f("AICAD_HEADROOM_GB", 4.0)) *
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
                      "(governor off or host unknown) knobs=AICAD_GOVERNOR,AI_CAD_SIM_THREADS,"
                      "AICAD_CPU_FRAC",
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
                  "knobs=AICAD_MEM_FRAC,AICAD_MEM_CAP_GB,AICAD_HEADROOM_GB,"
                  "AICAD_CPU_FRAC,AI_CAD_SIM_THREADS",
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

}  // namespace aicad::governor
