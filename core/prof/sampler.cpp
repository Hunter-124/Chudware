// Implementation of the in-process sampling profiler. See sampler.h for the design rationale.
//
// There are two real capture backends that share ONE platform-neutral aggregation + report
// core (so the report JSON shape is identical everywhere):
//   - Windows: heap-lock-safe capture via SuspendThread + RtlVirtualUnwind while the work
//     thread is suspended (reads only the module's memory-mapped .pdata — no heap, no DbgHelp,
//     so suspending mid-malloc can't deadlock us); DbgHelp symbolization deferred to report().
//   - Linux:   a dedicated sampler thread periodically delivers a real-time signal to the
//     registered work thread (syscall(SYS_tgkill)); an async-signal-safe handler walks that
//     thread's own stack with backtrace() into a lock-free SPSC ring; the sampler thread
//     drains the ring and folds each stack into the shared aggregation; dladdr +
//     abi::__cxa_demangle symbolization is deferred to report(), off the signal path.
//
// Shared (platform-neutral): the FNV-1a stack-hash -> count map (g_agg), the report builder
// (module/flat tables, folded text, speedscope), and the public API. Per-platform: the stack
// capture, the symbolizer, and the wall clock.

#include "prof/sampler.h"
#include "prof/tree.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <functional>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <dbghelp.h>

// Win10 1803+ high-resolution waitable-timer flag; define if the SDK header predates it.
#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif
#elif defined(__linux__)
#include <cerrno>
#include <cstdlib>
#include <cxxabi.h>
#include <dlfcn.h>
#include <elf.h>
#include <execinfo.h>
#include <link.h>  // dl_iterate_phdr
#include <pthread.h>
#include <signal.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#endif  // _WIN32 / __linux__

using nlohmann::json;

namespace chudware {
namespace prof {

namespace {

constexpr int kMaxFrames = 192;

#if defined(_WIN32) || defined(__linux__)
// ======================= shared aggregation + report core ============================
// A unique call stack (leaf-first) and how many times it was sampled. uintptr_t holds a code
// address on either platform (Win64 DWORD64 is the same 64-bit unsigned type).
struct Stack {
    std::vector<uintptr_t> addrs;  // index 0 = innermost (leaf) frame
    unsigned long long count = 0;
};

std::atomic<bool> g_running{false};
std::atomic<bool> g_stop{false};
std::thread g_thread;
unsigned g_interval_us = 1000;
std::string g_mode = "modeling";

std::mutex g_agg_mtx;
std::unordered_map<unsigned long long, Stack> g_agg;
std::atomic<unsigned long long> g_samples{0};

// Hash (FNV-1a over the leaf-first address vector) and fold one captured stack into g_agg.
// NOT async-signal-safe (locks + allocates) — call only from a normal thread (the Windows
// sampler thread, or the Linux ring drain), NEVER from inside a signal handler.
void aggregate_sample(const uintptr_t* pcs, int n) {
    if (n <= 0) return;
    unsigned long long hsh = 1469598103934665603ULL;  // FNV-1a over the address vector
    for (int i = 0; i < n; ++i) {
        hsh ^= static_cast<unsigned long long>(pcs[i]);
        hsh *= 1099511628211ULL;
    }
    {
        std::lock_guard<std::mutex> lk(g_agg_mtx);
        auto it = g_agg.find(hsh);
        if (it == g_agg.end()) {
            Stack st;
            st.addrs.assign(pcs, pcs + n);
            st.count = 1;
            g_agg.emplace(hsh, std::move(st));
        } else {
            it->second.count++;
        }
    }
    g_samples.fetch_add(1, std::memory_order_relaxed);
}

std::string module_of(const std::string& label) {
    auto p = label.find('!');
    return p == std::string::npos ? std::string("?") : label.substr(0, p);
}

// Cross-platform binary write (std::ofstream avoids both MSVC's fopen_s/C4996 friction and a
// POSIX fopen split).
bool write_text(const std::string& path, const std::string& text) {
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f.write(text.data(), static_cast<std::streamsize>(text.size()));
    return static_cast<bool>(f);
}

std::string build_speedscope(
    const std::vector<std::pair<std::vector<std::string>, unsigned long long>>& stacks,
    unsigned interval_us) {
    // speedscope "sampled" profile. Frames are deduped; each unique stack is one weighted
    // sample (weight = count * interval, in microseconds). Stacks are root->leaf.
    std::unordered_map<std::string, int> idx;
    std::vector<const std::string*> frames;
    auto frame_id = [&](const std::string& s) -> int {
        auto it = idx.find(s);
        if (it != idx.end()) return it->second;
        int id = static_cast<int>(frames.size());
        idx.emplace(s, id);
        frames.push_back(&s);
        return id;
    };
    json samples = json::array();
    json weights = json::array();
    unsigned long long end_value = 0;
    for (const auto& st : stacks) {
        json s = json::array();
        // st.first is root->leaf already (built that way by the caller)
        for (const auto& lbl : st.first) s.push_back(frame_id(lbl));
        samples.push_back(std::move(s));
        unsigned long long w = st.second * interval_us;
        weights.push_back(w);
        end_value += w;
    }
    json fr = json::array();
    for (auto* f : frames) fr.push_back(json{{"name", *f}});
    json doc = {
        {"$schema", "https://www.speedscope.app/file-format-schema.json"},
        {"name", "chudware-modeling"},
        {"activeProfileIndex", 0},
        {"shared", {{"frames", fr}}},
        {"profiles", json::array({json{
            {"type", "sampled"},
            {"name", "chudware modeling"},
            {"unit", "microseconds"},
            {"startValue", 0},
            {"endValue", end_value},
            {"samples", samples},
            {"weights", weights},
        }})}};
    return doc.dump();
}

// Snapshot the aggregation (safe to read while the sampler runs — report is usually after
// stop()). Fills `total` with the grand sample count.
std::vector<Stack> snapshot_stacks(unsigned long long& total) {
    std::vector<Stack> stacks;
    total = 0;
    std::lock_guard<std::mutex> lk(g_agg_mtx);
    stacks.reserve(g_agg.size());
    for (auto& kv : g_agg) {
        stacks.push_back(kv.second);
        total += kv.second.count;
    }
    return stacks;
}

// Platform-neutral report builder: turn a snapshot of aggregated stacks into the report JSON.
// `resolve_raw(addr, &unresolved)` is the per-platform symbolizer (DbgHelp / dladdr); the
// per-address cache lives here so it is invoked at most once per unique address.
json build_report(const json& opts, const std::vector<Stack>& stacks, unsigned long long total,
                  double wall,
                  const std::function<std::string(uintptr_t, unsigned long long*)>& resolve_raw) {
    int top = opts.value("top", 40);
    unsigned long long min_samples = opts.value("min_samples", 0);

    std::unordered_map<uintptr_t, std::string> cache;
    unsigned long long unresolved = 0;
    auto resolve = [&](uintptr_t a) -> const std::string& {
        auto it = cache.find(a);
        if (it != cache.end()) return it->second;
        std::string s = resolve_raw(a, &unresolved);
        return cache.emplace(a, std::move(s)).first->second;
    };

    std::unordered_map<std::string, unsigned long long> self_m, total_m, mod_m;
    std::string folded;
    // For speedscope: each unique stack as root->leaf labels + its count.
    std::vector<std::pair<std::vector<std::string>, unsigned long long>> ss_stacks;
    ss_stacks.reserve(stacks.size());

    for (const auto& st : stacks) {
        if (st.addrs.empty()) continue;
        std::vector<std::string> labels;  // leaf-first (matches st.addrs ordering)
        labels.reserve(st.addrs.size());
        for (uintptr_t a : st.addrs) labels.push_back(resolve(a));

        self_m[labels.front()] += st.count;
        mod_m[module_of(labels.front())] += st.count;

        std::unordered_set<std::string> seen;
        for (const auto& l : labels) {
            if (seen.insert(l).second) total_m[l] += st.count;
        }

        // folded: root -> leaf, ';'-separated, trailing " <count>"
        std::vector<std::string> root_first(labels.rbegin(), labels.rend());
        for (size_t i = 0; i < root_first.size(); ++i) {
            if (i) folded += ';';
            folded += root_first[i];
        }
        folded += ' ';
        folded += std::to_string(st.count);
        folded += '\n';

        ss_stacks.emplace_back(std::move(root_first), st.count);
    }

    auto to_sorted = [&](const std::unordered_map<std::string, unsigned long long>& m,
                         const char* key) {
        std::vector<std::pair<std::string, unsigned long long>> v(m.begin(), m.end());
        std::sort(v.begin(), v.end(), [](const auto& a, const auto& b) {
            return a.second > b.second;
        });
        json arr = json::array();
        int n = 0;
        for (auto& kv : v) {
            if (kv.second < min_samples) break;
            if (n++ >= top) break;
            arr.push_back(json{{"frame", kv.first},
                               {key, kv.second},
                               {"pct", 100.0 * double(kv.second) / double(total)}});
        }
        return arr;
    };

    // module table (no top cap — there are only a handful of modules)
    std::vector<std::pair<std::string, unsigned long long>> mods(mod_m.begin(), mod_m.end());
    std::sort(mods.begin(), mods.end(),
              [](const auto& a, const auto& b) { return a.second > b.second; });
    json mod_arr = json::array();
    for (auto& kv : mods) {
        mod_arr.push_back(json{{"module", kv.first},
                               {"self", kv.second},
                               {"pct", 100.0 * double(kv.second) / double(total)}});
    }

    json wrote = json::array();
    if (opts.contains("write_folded") && opts["write_folded"].is_string()) {
        std::string p = opts["write_folded"].get<std::string>();
        if (write_text(p, folded)) wrote.push_back(p);
    }
    if (opts.contains("write_speedscope") && opts["write_speedscope"].is_string()) {
        std::string p = opts["write_speedscope"].get<std::string>();
        if (write_text(p, build_speedscope(ss_stacks, g_interval_us))) wrote.push_back(p);
    }

    return json{
        {"ok", true},
        {"summary",
         {{"samples", total},
          {"unique_stacks", stacks.size()},
          {"interval_us", g_interval_us},
          {"mode", g_mode},
          {"wall_seconds", wall},
          {"unresolved_frames", unresolved}}},
        {"modules", mod_arr},
        {"flat_self", to_sorted(self_m, "self")},
        {"flat_total", to_sorted(total_m, "total")},
        {"folded", folded},
        {"wrote", wrote}};
}
#endif  // _WIN32 || __linux__

#ifdef _WIN32
// ============================== Windows capture backend ==============================
std::mutex g_target_mtx;
HANDLE g_target = nullptr;  // duplicated, real (non-pseudo) handle to the work thread
DWORD g_target_tid = 0;
int g_target_depth = 0;

LARGE_INTEGER g_qpf{};
long long g_start_qpc = 0;
long long g_stop_qpc = 0;

double wall_seconds() {
    if (g_qpf.QuadPart == 0) return 0.0;
    long long end = g_stop_qpc;
    if (end == 0) {
        LARGE_INTEGER t;
        QueryPerformanceCounter(&t);
        end = t.QuadPart;
    }
    return double(end - g_start_qpc) / double(g_qpf.QuadPart);
}

#if defined(_M_X64)
// Lock-free, allocation-free x64 stack walk. Reads only the module's memory-mapped .pdata
// unwind info (RtlLookupFunctionEntry/RtlVirtualUnwind) and the suspended thread's own
// stack memory — never the heap, never DbgHelp — so suspending the target mid-malloc can't
// deadlock us. POD-only so the SEH guard in capture_locked is legal (MSVC C2712).
int unwind_walk(CONTEXT* ctx, DWORD64* pcs, int maxf) {
    int n = 0;
    while (n < maxf) {
        DWORD64 pc = ctx->Rip;
        if (!pc) break;
        pcs[n++] = pc;
        DWORD64 base = 0;
        PRUNTIME_FUNCTION fn = RtlLookupFunctionEntry(pc, &base, nullptr);
        if (!fn) {
            // Leaf function (no unwind data): the return address sits at [Rsp].
            if (!ctx->Rsp) break;
            DWORD64 ret = *reinterpret_cast<DWORD64*>(ctx->Rsp);
            ctx->Rip = ret;
            ctx->Rsp += 8;
            if (!ret) break;
            continue;
        }
        PVOID handler_data = nullptr;
        DWORD64 establisher = 0;
        RtlVirtualUnwind(UNW_FLAG_NHANDLER, base, pc, fn, ctx, &handler_data, &establisher,
                         nullptr);
        if (!ctx->Rip) break;
    }
    return n;
}
#endif  // _M_X64

// Called with the target thread already suspended. POD-only; guards the raw stack reads in
// unwind_walk with SEH so a momentarily-inconsistent stack yields a partial sample rather
// than crashing the profiler.
int capture_locked(HANDLE h, DWORD64* pcs, int maxf) {
#if defined(_M_X64)
    CONTEXT ctx;
    ZeroMemory(&ctx, sizeof(ctx));
    ctx.ContextFlags = CONTEXT_FULL;
    int n = 0;
    if (GetThreadContext(h, &ctx)) {
        __try {
            n = unwind_walk(&ctx, pcs, maxf);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            // keep whatever frames were captured before the fault
        }
    }
    return n;
#else
    (void)h;
    (void)pcs;
    (void)maxf;
    return 0;
#endif
}

void sampler_main() {
    HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr,
                                          CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                                          TIMER_ALL_ACCESS);
    if (!timer) timer = CreateWaitableTimerExW(nullptr, nullptr, 0, TIMER_ALL_ACCESS);
    DWORD64 pcs[kMaxFrames];
    // Bound the wait so g_stop is observed within ~one interval (a few ms floor) even if the
    // timer misbehaves; cadence jitter is irrelevant to a statistical sampler.
    DWORD wait_ms = (g_interval_us / 1000) + 4;

    while (!g_stop.load(std::memory_order_relaxed)) {
        if (timer) {
            LARGE_INTEGER due;
            due.QuadPart = -static_cast<LONGLONG>(static_cast<long long>(g_interval_us) * 10);
            SetWaitableTimer(timer, &due, 0, nullptr, nullptr, FALSE);
            WaitForSingleObject(timer, wait_ms);
        } else {
            std::this_thread::sleep_for(std::chrono::microseconds(g_interval_us));
        }

        HANDLE h = nullptr;
        DWORD tid = 0;
        {
            std::lock_guard<std::mutex> lk(g_target_mtx);
            h = g_target;
            tid = g_target_tid;
        }
        if (!h) continue;                          // no modeling op in flight -> idle
        if (tid == GetCurrentThreadId()) continue;  // never sample the sampler

        if (SuspendThread(h) == static_cast<DWORD>(-1)) continue;
        int n = capture_locked(h, pcs, kMaxFrames);
        ResumeThread(h);
        if (n <= 0) continue;

        aggregate_sample(reinterpret_cast<const uintptr_t*>(pcs), n);  // shared aggregation
    }
    if (timer) CloseHandle(timer);
}

// --- symbolization (report path only; never while a thread is suspended) -----------
std::once_flag g_sym_once;
bool g_sym_ok = false;

void ensure_sym() {
    std::call_once(g_sym_once, []() {
        SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_LOAD_LINES);
        // invade=TRUE enumerates already-loaded modules (libchudware, TK*, gmsh, ...). If the
        // host already SymInitialize'd, this returns FALSE; we proceed regardless and never
        // SymCleanup (which would break the host's session).
        g_sym_ok = SymInitialize(GetCurrentProcess(), nullptr, TRUE) != FALSE;
    });
}

std::string resolve_addr(DWORD64 a, unsigned long long* unresolved) {
    HANDLE proc = GetCurrentProcess();
    DWORD64 base = SymGetModuleBase64(proc, a);
    std::string modname;
    if (base) {
        IMAGEHLP_MODULE64 mi;
        ZeroMemory(&mi, sizeof(mi));
        mi.SizeOfStruct = sizeof(IMAGEHLP_MODULE64);
        if (SymGetModuleInfo64(proc, base, &mi)) modname = mi.ModuleName;
    }

    alignas(8) char symbuf[sizeof(SYMBOL_INFO) + MAX_SYM_NAME];
    auto* sym = reinterpret_cast<SYMBOL_INFO*>(symbuf);
    ZeroMemory(symbuf, sizeof(symbuf));
    sym->SizeOfStruct = sizeof(SYMBOL_INFO);
    sym->MaxNameLen = MAX_SYM_NAME;
    DWORD64 disp = 0;
    if (base && SymFromAddr(proc, a, &disp, sym) && sym->NameLen > 0) {
        std::string name(sym->Name, sym->NameLen);
        return (modname.empty() ? std::string("?") : modname) + "!" + name;
    }

    if (unresolved) (*unresolved)++;
    char tail[40];
    if (base) {
        std::snprintf(tail, sizeof(tail), "!0x%llx",
                      static_cast<unsigned long long>(a - base));
        return (modname.empty() ? std::string("?") : modname) + tail;
    }
    std::snprintf(tail, sizeof(tail), "0x%llx", static_cast<unsigned long long>(a));
    return std::string(tail);
}

#elif defined(__linux__)
// =============================== Linux capture backend ===============================
// ---- target registration (the work thread's kernel tid) ----
std::mutex g_target_mtx;
pid_t g_target_tid = 0;   // 0 = no work thread registered -> sampler idles
int g_target_depth = 0;   // ref-count for nested modeling scopes on the same thread

// ---- wall clock (steady_clock; QPC has no portable analogue) ----
std::chrono::steady_clock::time_point g_start_tp{};
std::chrono::steady_clock::time_point g_stop_tp{};
bool g_have_stop_tp = false;

double wall_seconds() {
    if (g_start_tp.time_since_epoch().count() == 0) return 0.0;
    auto end = g_have_stop_tp ? g_stop_tp : std::chrono::steady_clock::now();
    return std::chrono::duration<double>(end - g_start_tp).count();
}

// ---- the sampling signal + saved previous disposition ----
int g_sig = 0;                     // resolved in start() (SIGRTMIN is not a constant)
struct sigaction g_old_sa;         // previous handler, restored in stop()
bool g_old_sa_valid = false;
std::atomic<bool> g_armed{false};  // handler no-ops once disarmed (stop()'s late-signal guard)

// ---- lock-free single-producer/single-consumer ring of captured stacks --------------
// Producer = the signal handler (runs on the work thread); consumer = the sampler/drain
// thread. The handler only writes preallocated static storage + bumps one atomic — no malloc,
// no locks — so it stays async-signal-safe.
constexpr unsigned kRingSize = 128;  // power of two; the drain runs every interval so depth ~0
struct RingSlot {
    int n = 0;
    void* frames[kMaxFrames];
};
RingSlot g_ring[kRingSize];
std::atomic<uint64_t> g_ring_head{0};          // next write index (producer)
std::atomic<uint64_t> g_ring_tail{0};          // next read index  (consumer)
std::atomic<unsigned long long> g_dropped{0};  // samples lost to a full ring (diagnostic)
std::atomic<unsigned long long> g_ticks{0};    // sampler wakeups
std::atomic<unsigned long long> g_ticks_hit{0}; // wakeups that had a live target

void sample_handler(int /*sig*/, siginfo_t* /*si*/, void* uc) {
    if (!g_armed.load(std::memory_order_acquire)) return;  // stopped -> no-op (restore-race guard)
    int saved_errno = errno;
    uint64_t head = g_ring_head.load(std::memory_order_relaxed);
    uint64_t tail = g_ring_tail.load(std::memory_order_acquire);
    if (head - tail >= kRingSize) {
        g_dropped.fetch_add(1, std::memory_order_relaxed);  // ring full -> drop this sample
        errno = saved_errno;
        return;
    }
    RingSlot& slot = g_ring[head & (kRingSize - 1)];
    // Leaf = the interrupted PC from ucontext (NOT sample_handler). Plain backtrace()
    // alone leaves every sample's self-time on the handler, which made the profiler
    // look "symbolized but blind".
    int n = 0;
#if defined(__x86_64__)
    if (uc) {
        auto* ctx = static_cast<ucontext_t*>(uc);
        slot.frames[n++] = reinterpret_cast<void*>(ctx->uc_mcontext.gregs[REG_RIP]);
    }
#elif defined(__aarch64__)
    if (uc) {
        auto* ctx = static_cast<ucontext_t*>(uc);
        slot.frames[n++] = reinterpret_cast<void*>(ctx->uc_mcontext.pc);
    }
#endif
    // Remainder of the stack via backtrace; skip the top frames that live in this
    // handler / the libc signal trampoline so we don't double-count the leaf.
    void* tmp[kMaxFrames];
    int bn = backtrace(tmp, kMaxFrames);  // async-signal-safe after warm-up
    int skip = 0;
    // Skip frames until we pass sample_handler (and usually one glibc trampoline).
    for (int i = 0; i < bn && skip < 4; ++i) {
        // Always skip the first frame (this function) and the next couple of
        // signal-delivery frames; the ucontext PC already covers the true leaf.
        skip = i + 1;
        if (i >= 1) break;
    }
    if (bn > 2) skip = 2;  // handler + sigreturn is the common Linux layout
    for (int i = skip; i < bn && n < kMaxFrames; ++i)
        slot.frames[n++] = tmp[i];
    if (n <= 0 && bn > 0) {
        // Fallback: raw backtrace if ucontext path produced nothing.
        n = bn < kMaxFrames ? bn : kMaxFrames;
        for (int i = 0; i < n; ++i) slot.frames[i] = tmp[i];
    }
    slot.n = n;
    g_ring_head.store(head + 1, std::memory_order_release);  // publish the filled slot
    errno = saved_errno;
}

// Drain every published ring slot into the shared aggregation. Runs ONLY on the sampler
// thread, so the locking/allocating aggregate_sample is safe here.
void drain_ring() {
    uint64_t tail = g_ring_tail.load(std::memory_order_relaxed);
    uint64_t head = g_ring_head.load(std::memory_order_acquire);
    uintptr_t pcs[kMaxFrames];
    for (; tail != head; ++tail) {
        RingSlot& slot = g_ring[tail & (kRingSize - 1)];
        int n = slot.n;
        if (n > kMaxFrames) n = kMaxFrames;
        for (int i = 0; i < n; ++i)
            pcs[i] = reinterpret_cast<uintptr_t>(slot.frames[i]);
        g_ring_tail.store(tail + 1, std::memory_order_release);  // free the slot (already copied)
        aggregate_sample(pcs, n);
    }
}

void sampler_main() {
    // Absolute-time cadence: sleep_for under load drifts badly (observed ~1.7 Hz
    // instead of 1 kHz). clock_nanosleep(TIMER_ABSTIME) keeps the schedule even when the
    // box is busy; catch-up is capped so a long stall does not storm-signal.
    using clock = std::chrono::steady_clock;
    auto next = clock::now();
    const auto step = std::chrono::microseconds(g_interval_us ? g_interval_us : 1000u);
    const int kMaxCatchUp = 8;
    while (!g_stop.load(std::memory_order_relaxed)) {
        next += step;
        auto now = clock::now();
        if (next > now) {
            auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(next - now);
            struct timespec ts;
            ts.tv_sec = static_cast<time_t>(ns.count() / 1000000000LL);
            ts.tv_nsec = static_cast<long>(ns.count() % 1000000000LL);
            // TIMER_ABSTIME against CLOCK_MONOTONIC matches steady_clock on Linux.
            auto abs = std::chrono::duration_cast<std::chrono::nanoseconds>(
                next.time_since_epoch());
            struct timespec abs_ts;
            abs_ts.tv_sec = static_cast<time_t>(abs.count() / 1000000000LL);
            abs_ts.tv_nsec = static_cast<long>(abs.count() % 1000000000LL);
            while (!g_stop.load(std::memory_order_relaxed)) {
                int rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &abs_ts, nullptr);
                if (rc == 0 || rc != EINTR) break;
            }
            (void)ts;
        } else {
            // We're behind — skip at most kMaxCatchUp intervals so one long pause does not
            // dump a burst of signals into a short window.
            int behind = 0;
            while (next + step <= clock::now() && behind < kMaxCatchUp) {
                next += step;
                ++behind;
            }
        }
        if (g_stop.load(std::memory_order_relaxed)) break;
        g_ticks.fetch_add(1, std::memory_order_relaxed);
        pid_t tid = 0;
        {
            std::lock_guard<std::mutex> lk(g_target_mtx);
            tid = g_target_tid;
        }
        if (tid != 0) {
            g_ticks_hit.fetch_add(1, std::memory_order_relaxed);
            // Deliver the sampling signal to the work thread (intra-process, by kernel tid).
            // ESRCH (the thread just exited) and friends are ignored — statistical sampler.
            syscall(SYS_tgkill, getpid(), tid, g_sig);
        }
        drain_ring();
    }
    drain_ring();  // final drain of any samples captured in the last interval
}

// ---- symbolization (report path only; off the signal path) --------------------------
// basename(path), stripped of a leading "lib" and a trailing ".so*": libTKMath.so.7.6 ->
// "TKMath", libchudware.so -> "chudware", and an executable like /path/test_prof -> "test_prof".
std::string clean_module(const char* path) {
    std::string p = path ? path : "";
    auto slash = p.find_last_of('/');
    std::string base = (slash == std::string::npos) ? p : p.substr(slash + 1);
    auto so = base.find(".so");
    if (so != std::string::npos) base = base.substr(0, so);
    if (base.rfind("lib", 0) == 0) base = base.substr(3);
    return base.empty() ? std::string("?") : base;
}

// ELF .symtab/.dynsym lookup for addresses where dladdr has no dli_sname (static /
// hidden symbols still present in an unstripped .so). Built once per module path.
struct ElfSym {
    uintptr_t addr = 0;  // st_value as loaded (file vaddr; we add load bias at lookup)
    uintptr_t size = 0;
    std::string name;
};
struct ElfMod {
    std::string path;
    uintptr_t load_bias = 0;  // actual map base - ELF preferred base (for ET_DYN)
    std::vector<ElfSym> syms; // sorted by addr
    bool loaded = false;
};
std::mutex g_elf_mtx;
std::unordered_map<std::string, ElfMod> g_elf_mods;

bool load_elf_syms(ElfMod& mod) {
    if (mod.loaded) return !mod.syms.empty();
    mod.loaded = true;
    std::ifstream f(mod.path, std::ios::binary);
    if (!f) return false;
    Elf64_Ehdr eh{};
    f.read(reinterpret_cast<char*>(&eh), sizeof(eh));
    if (!f || eh.e_ident[EI_MAG0] != ELFMAG0 || eh.e_ident[EI_MAG1] != ELFMAG1 ||
        eh.e_ident[EI_MAG2] != ELFMAG2 || eh.e_ident[EI_MAG3] != ELFMAG3 ||
        eh.e_ident[EI_CLASS] != ELFCLASS64) {
        return false;
    }
    // Prefer .symtab (full) then fall back to .dynsym.
    std::vector<char> shstr;
    {
        f.seekg(static_cast<std::streamoff>(eh.e_shoff +
                                            eh.e_shstrndx * eh.e_shentsize));
        Elf64_Shdr shstr_hdr{};
        f.read(reinterpret_cast<char*>(&shstr_hdr), sizeof(shstr_hdr));
        if (!f || shstr_hdr.sh_size == 0) return false;
        shstr.resize(static_cast<size_t>(shstr_hdr.sh_size));
        f.seekg(static_cast<std::streamoff>(shstr_hdr.sh_offset));
        f.read(shstr.data(), static_cast<std::streamsize>(shstr.size()));
        if (!f) return false;
    }
    Elf64_Shdr symtab{}, strtab{};
    bool have = false;
    for (int pass = 0; pass < 2 && !have; ++pass) {
        const char* want = (pass == 0) ? ".symtab" : ".dynsym";
        const char* wantstr = (pass == 0) ? ".strtab" : ".dynstr";
        Elf64_Shdr found_sym{}, found_str{};
        bool got_sym = false, got_str = false;
        for (uint16_t i = 0; i < eh.e_shnum; ++i) {
            f.seekg(static_cast<std::streamoff>(eh.e_shoff + i * eh.e_shentsize));
            Elf64_Shdr sh{};
            f.read(reinterpret_cast<char*>(&sh), sizeof(sh));
            if (!f) return false;
            if (sh.sh_name >= shstr.size()) continue;
            const char* nm = shstr.data() + sh.sh_name;
            if (std::strcmp(nm, want) == 0) {
                found_sym = sh;
                got_sym = true;
            }
            if (std::strcmp(nm, wantstr) == 0) {
                found_str = sh;
                got_str = true;
            }
        }
        if (got_sym && got_str) {
            symtab = found_sym;
            strtab = found_str;
            have = true;
        }
    }
    if (!have || symtab.sh_entsize == 0) return false;
    std::vector<char> strings(static_cast<size_t>(strtab.sh_size));
    f.seekg(static_cast<std::streamoff>(strtab.sh_offset));
    f.read(strings.data(), static_cast<std::streamsize>(strings.size()));
    if (!f) return false;
    const size_t nsym = static_cast<size_t>(symtab.sh_size / symtab.sh_entsize);
    mod.syms.reserve(nsym);
    for (size_t i = 0; i < nsym; ++i) {
        f.seekg(static_cast<std::streamoff>(symtab.sh_offset + i * symtab.sh_entsize));
        Elf64_Sym s{};
        f.read(reinterpret_cast<char*>(&s), sizeof(s));
        if (!f) break;
        if (s.st_name == 0 || s.st_value == 0) continue;
        if (ELF64_ST_TYPE(s.st_info) != STT_FUNC && ELF64_ST_TYPE(s.st_info) != STT_NOTYPE)
            continue;
        if (s.st_name >= strings.size()) continue;
        ElfSym es;
        es.addr = static_cast<uintptr_t>(s.st_value);
        es.size = static_cast<uintptr_t>(s.st_size);
        es.name = strings.data() + s.st_name;
        if (es.name.empty() || es.name[0] == '$') continue;
        mod.syms.push_back(std::move(es));
    }
    std::sort(mod.syms.begin(), mod.syms.end(),
              [](const ElfSym& a, const ElfSym& b) { return a.addr < b.addr; });
    return !mod.syms.empty();
}

// Map an absolute address to a symbol via the module's ELF table. `fbase` is dladdr's
// dli_fbase (load base of the first PT_LOAD). For ET_DYN, st_value is relative to the
// ELF preferred base (usually 0), so load_bias ≈ fbase.
std::string elf_lookup(const char* path, void* fbase, uintptr_t addr) {
    if (!path || !fbase) return {};
    std::lock_guard<std::mutex> lk(g_elf_mtx);
    ElfMod& mod = g_elf_mods[path];
    if (mod.path.empty()) mod.path = path;
    mod.load_bias = reinterpret_cast<uintptr_t>(fbase);
    if (!load_elf_syms(mod)) return {};
    const uintptr_t rel = addr - mod.load_bias;
    // Greatest symbol address <= rel (and within size if size known).
    auto it = std::upper_bound(mod.syms.begin(), mod.syms.end(), rel,
                               [](uintptr_t v, const ElfSym& s) { return v < s.addr; });
    if (it == mod.syms.begin()) return {};
    --it;
    if (it->size && rel >= it->addr + it->size) return {};
    return it->name;
}

std::string demangle(const std::string& sname) {
    if (sname.empty()) return {};
    int status = 0;
    char* dem = abi::__cxa_demangle(sname.c_str(), nullptr, nullptr, &status);
    std::string sym = (status == 0 && dem) ? std::string(dem) : sname;
    std::free(dem);
    return sym;
}

std::string resolve_addr(uintptr_t a, unsigned long long* unresolved) {
    Dl_info info;
    if (dladdr(reinterpret_cast<void*>(a), &info) && info.dli_fname) {
        std::string mod = clean_module(info.dli_fname);
        std::string sname = info.dli_sname ? std::string(info.dli_sname) : std::string();
        // dladdr only sees .dynsym. Walk .symtab for static / hidden frames
        // so unstripped libchudware.so reports real function names instead of chudware!0x….
        if (sname.empty()) sname = elf_lookup(info.dli_fname, info.dli_fbase, a);
        if (!sname.empty()) {
            return mod + "!" + demangle(sname);
        }
        // In-module address with no symbol -> module!0x<rva> (rva from the module load base).
        if (unresolved) (*unresolved)++;
        unsigned long long rva = static_cast<unsigned long long>(
            a - reinterpret_cast<uintptr_t>(info.dli_fbase));
        char tail[40];
        std::snprintf(tail, sizeof(tail), "!0x%llx", rva);
        return mod + tail;
    }
    if (unresolved) (*unresolved)++;
    char buf[40];
    std::snprintf(buf, sizeof(buf), "0x%llx", static_cast<unsigned long long>(a));
    return std::string(buf);
}
#endif  // _WIN32 / __linux__

}  // namespace

// ----------------------------------------------------------------- public API

bool start(unsigned interval_us, const std::string& mode) {
#ifdef _WIN32
    if (g_running.exchange(true)) return false;  // already running
    g_stop.store(false);
    g_interval_us = interval_us ? std::max(interval_us, 100u) : 1000u;  // floor 100us = 10kHz
    g_mode = mode.empty() ? std::string("modeling") : mode;
    {
        std::lock_guard<std::mutex> lk(g_agg_mtx);
        g_agg.clear();
    }
    g_samples.store(0);
    QueryPerformanceFrequency(&g_qpf);
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    g_start_qpc = t.QuadPart;
    g_stop_qpc = 0;
    g_thread = std::thread(sampler_main);
    return true;
#elif defined(__linux__)
    if (g_running.exchange(true)) return false;  // already running
    g_stop.store(false);
    g_interval_us = interval_us ? std::max(interval_us, 100u) : 1000u;  // floor 100us = 10kHz
    g_mode = mode.empty() ? std::string("modeling") : mode;
    {
        std::lock_guard<std::mutex> lk(g_agg_mtx);
        g_agg.clear();
    }
    g_samples.store(0);
    g_dropped.store(0);
    g_ticks.store(0);
    g_ticks_hit.store(0);
    g_ring_head.store(0);
    g_ring_tail.store(0);
    // Warm up libgcc's unwinder so the FIRST backtrace() inside the signal handler does NOT
    // dlopen libgcc_s (not async-signal-safe). After this, in-handler backtrace only touches
    // already-loaded code.
    {
        void* warm[8];
        (void)backtrace(warm, 8);
    }
    // Install the sampling-signal handler, saving the previous disposition for stop(). A
    // real-time signal (queued, clear of the standard signals + glibc's NPTL-reserved few).
    g_sig = SIGRTMIN + 6;
    if (g_sig > SIGRTMAX) g_sig = SIGRTMIN;
    struct sigaction sa {};
    sa.sa_sigaction = &sample_handler;
    sa.sa_flags = SA_SIGINFO | SA_RESTART;
    sigemptyset(&sa.sa_mask);
    g_old_sa_valid = (sigaction(g_sig, &sa, &g_old_sa) == 0);
    if (!g_old_sa_valid) {
        g_running.store(false);
        return false;
    }
    g_armed.store(true, std::memory_order_release);
    g_start_tp = std::chrono::steady_clock::now();
    g_have_stop_tp = false;
    g_thread = std::thread(sampler_main);
    // Best-effort: name the sampler + try a mild priority boost so under load it still
    // wakes near the requested cadence (the original sleep_for path starved to ~1.7 Hz).
    {
        pthread_setname_np(g_thread.native_handle(), "chudware-prof");
#if defined(__linux__)
        struct sched_param sp {};
        sp.sched_priority = 0;
        // SCHED_OTHER with negative nice if permitted; ignore failures (containers).
        (void)pthread_setschedparam(g_thread.native_handle(), SCHED_OTHER, &sp);
#endif
    }
    return true;
#else
    (void)interval_us;
    (void)mode;
    return false;
#endif
}

void stop() {
#ifdef _WIN32
    if (!g_running.exchange(false)) return;
    g_stop.store(true);
    if (g_thread.joinable()) g_thread.join();
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    g_stop_qpc = t.QuadPart;
    std::lock_guard<std::mutex> lk(g_target_mtx);
    if (g_target) {
        CloseHandle(g_target);
        g_target = nullptr;
    }
    g_target_tid = 0;
    g_target_depth = 0;
#elif defined(__linux__)
    if (!g_running.exchange(false)) return;
    g_stop.store(true);
    if (g_thread.joinable()) g_thread.join();  // sampler stops signalling + does a final drain
    g_stop_tp = std::chrono::steady_clock::now();
    g_have_stop_tp = true;
    {
        std::lock_guard<std::mutex> lk(g_target_mtx);
        g_target_tid = 0;
        g_target_depth = 0;
    }
    // Disarm FIRST so a still-in-flight signal hits our now-no-op handler, THEN restore the
    // previous disposition (a stray RT signal arriving after this is astronomically unlikely —
    // the target was last signalled an interval + a thread-join ago).
    g_armed.store(false, std::memory_order_release);
    if (g_old_sa_valid) {
        sigaction(g_sig, &g_old_sa, nullptr);
        g_old_sa_valid = false;
    }
#endif
}

bool active() {
#if defined(_WIN32) || defined(__linux__)
    return g_running.load();
#else
    return false;
#endif
}

unsigned long long sample_count() {
#if defined(_WIN32) || defined(__linux__)
    return g_samples.load();
#else
    return 0;
#endif
}

unsigned interval_us() {
#if defined(_WIN32) || defined(__linux__)
    return g_interval_us;
#else
    return 0;
#endif
}

std::string mode() {
#if defined(_WIN32) || defined(__linux__)
    return g_mode;
#else
    return "";
#endif
}

void set_target() {
#ifdef _WIN32
    if (!g_running.load()) return;
    std::lock_guard<std::mutex> lk(g_target_mtx);
    DWORD me = GetCurrentThreadId();
    if (g_target_depth > 0 && g_target_tid == me) {
        g_target_depth++;  // nested modeling scope on the same thread
        return;
    }
    // Modeling is serialized under the global doc lock, so a live target from another thread
    // shouldn't exist; replace defensively if it does.
    if (g_target) {
        CloseHandle(g_target);
        g_target = nullptr;
    }
    HANDLE dup = nullptr;
    DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &dup,
                    THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION,
                    FALSE, 0);
    g_target = dup;
    g_target_tid = me;
    g_target_depth = 1;
#elif defined(__linux__)
    if (!g_running.load()) return;
    std::lock_guard<std::mutex> lk(g_target_mtx);
    pid_t me = static_cast<pid_t>(syscall(SYS_gettid));
    if (g_target_depth > 0 && g_target_tid == me) {
        g_target_depth++;  // nested modeling scope on the same thread
        return;
    }
    g_target_tid = me;  // modeling is serialized; replace any stale target defensively
    g_target_depth = 1;
    // Ensure the sampling signal can actually be delivered to THIS (work) thread even if the
    // host masked it. Safe here — normal context, not a signal handler.
    sigset_t s;
    sigemptyset(&s);
    sigaddset(&s, g_sig);
    pthread_sigmask(SIG_UNBLOCK, &s, nullptr);
#endif
}

void clear_target() {
#ifdef _WIN32
    std::lock_guard<std::mutex> lk(g_target_mtx);
    if (g_target_depth > 0) {
        g_target_depth--;
        if (g_target_depth == 0) {
            if (g_target) {
                CloseHandle(g_target);
                g_target = nullptr;
            }
            g_target_tid = 0;
        }
    }
#elif defined(__linux__)
    std::lock_guard<std::mutex> lk(g_target_mtx);
    if (g_target_depth > 0) {
        g_target_depth--;
        if (g_target_depth == 0) g_target_tid = 0;
    }
#endif
}

json report(const json& opts) {
#if defined(_WIN32) || defined(__linux__)
    // Snapshot the aggregation (safe to read while the sampler runs; report is usually after
    // stop()).
    unsigned long long total = 0;
    std::vector<Stack> stacks = snapshot_stacks(total);
    double wall = wall_seconds();
    if (total == 0) {
        return json{{"ok", false},
                    {"error",
                     "no samples captured — was a modeling op actually running while the "
                     "profiler was active? (in modeling mode only doc_* ops are sampled)"}};
    }
#ifdef _WIN32
    ensure_sym();
    SymRefreshModuleList(GetCurrentProcess());
    return build_report(opts, stacks, total, wall,
                        [](uintptr_t a, unsigned long long* u) -> std::string {
                            return resolve_addr(static_cast<DWORD64>(a), u);
                        });
#else  // __linux__
    json r = build_report(opts, stacks, total, wall,
                          [](uintptr_t a, unsigned long long* u) -> std::string {
                              return resolve_addr(a, u);
                          });
    // Cadence diagnostics: ticks = sampler wakeups, ticks_with_target =
    // wakeups that found a registered work thread, dropped = ring overflows.
    if (r.contains("summary") && r["summary"].is_object()) {
        r["summary"]["ticks"] = g_ticks.load();
        r["summary"]["ticks_with_target"] = g_ticks_hit.load();
        r["summary"]["dropped"] = g_dropped.load();
    }
    return r;
#endif
#else
    (void)opts;
    return json{{"ok", false}, {"error", "sampling profiler is unsupported on this platform"}};
#endif
}

json tree_report(const json& opts) {
    // Pull a fresh symbolized report for the folded text ONLY — never forward write_*
    // opts so this call cannot clobber files the caller didn't ask for.
    json flat = report(json::object());
    if (!flat.value("ok", false)) {
        return flat;  // preserve ok:false / error envelope (no samples / unsupported)
    }
    const std::string folded = flat.value("folded", std::string());
    json tree_opts = json::object();
    if (opts.contains("path")) tree_opts["path"] = opts["path"];
    if (opts.contains("depth")) tree_opts["depth"] = opts["depth"];
    if (opts.contains("top")) tree_opts["top"] = opts["top"];
    json out = tree::from_folded(folded, tree_opts);
    if (flat.contains("summary")) out["summary"] = flat["summary"];
    if (flat.contains("modules")) out["modules"] = flat["modules"];
    return out;
}

}  // namespace prof
}  // namespace chudware
