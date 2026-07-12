// resource_governor.cpp — see resource_governor.h.
#include "util/resource_governor.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#elif defined(__APPLE__)
#include <mach/mach.h>
#include <sys/sysctl.h>
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
    if (estimate_bytes <= budget) return true;
    const HostMem m = host_mem();
    char buf[352];
    std::snprintf(buf, sizeof buf,
                  "%s needs an estimated %.1f GB but this machine's budget is %.1f GB "
                  "(%.0f GB RAM, %.1f GB available) — use a coarser mesh / fewer cells / "
                  "first-order elements, or raise AICAD_MEM_FRAC / AICAD_MEM_CAP_GB",
                  what && what[0] ? what : "this study", gb(estimate_bytes), gb(budget),
                  gb(m.total), gb(m.avail));
    why = buf;
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
    if (const char* e = std::getenv("AI_CAD_SIM_THREADS")) {
        const int v = std::atoi(e);
        if (v > 0) return v;
    }
    const unsigned hc = std::thread::hardware_concurrency();
    return std::max(1, static_cast<int>(hc ? hc * 3 / 4 : 1));
}

}  // namespace aicad::governor
