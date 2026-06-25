// Implementation of the in-process sampling profiler. See sampler.h for the design
// rationale (heap-lock-safe capture via RtlVirtualUnwind while suspended; DbgHelp
// symbolization deferred to report()).

#include "prof/sampler.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
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
#endif  // _WIN32

using nlohmann::json;

namespace aicad {
namespace prof {

namespace {

constexpr int kMaxFrames = 192;

#ifdef _WIN32
struct Stack {
    std::vector<DWORD64> addrs;  // index 0 = innermost (leaf) frame
    unsigned long long count = 0;
};

std::atomic<bool> g_running{false};
std::atomic<bool> g_stop{false};
std::thread g_thread;
unsigned g_interval_us = 1000;
std::string g_mode = "modeling";

std::mutex g_target_mtx;
HANDLE g_target = nullptr;  // duplicated, real (non-pseudo) handle to the work thread
DWORD g_target_tid = 0;
int g_target_depth = 0;

std::mutex g_agg_mtx;
std::unordered_map<unsigned long long, Stack> g_agg;
std::atomic<unsigned long long> g_samples{0};

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
    if (timer) CloseHandle(timer);
}

// --- symbolization (report path only; never while a thread is suspended) -----------
std::once_flag g_sym_once;
bool g_sym_ok = false;

void ensure_sym() {
    std::call_once(g_sym_once, []() {
        SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_LOAD_LINES);
        // invade=TRUE enumerates already-loaded modules (libaicad, TK*, gmsh, ...). If the
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

std::string module_of(const std::string& label) {
    auto p = label.find('!');
    return p == std::string::npos ? std::string("?") : label.substr(0, p);
}

bool write_text(const std::string& path, const std::string& text) {
    FILE* f = nullptr;
    if (fopen_s(&f, path.c_str(), "wb") != 0 || !f) return false;
    fwrite(text.data(), 1, text.size(), f);
    fclose(f);
    return true;
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
        {"name", "aicad-modeling"},
        {"activeProfileIndex", 0},
        {"shared", {{"frames", fr}}},
        {"profiles", json::array({json{
            {"type", "sampled"},
            {"name", "aicad modeling"},
            {"unit", "microseconds"},
            {"startValue", 0},
            {"endValue", end_value},
            {"samples", samples},
            {"weights", weights},
        }})}};
    return doc.dump();
}
#endif  // _WIN32

}  // namespace

// ----------------------------------------------------------------- public API

bool start(unsigned interval_us, const std::string& mode) {
#ifndef _WIN32
    (void)interval_us;
    (void)mode;
    return false;
#else
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
#endif
}

bool active() {
#ifdef _WIN32
    return g_running.load();
#else
    return false;
#endif
}

unsigned long long sample_count() {
#ifdef _WIN32
    return g_samples.load();
#else
    return 0;
#endif
}

unsigned interval_us() {
#ifdef _WIN32
    return g_interval_us;
#else
    return 0;
#endif
}

std::string mode() {
#ifdef _WIN32
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
#endif
}

json report(const json& opts) {
#ifndef _WIN32
    (void)opts;
    return json{{"ok", false}, {"error", "sampling profiler is Windows-only"}};
#else
    int top = opts.value("top", 40);
    unsigned long long min_samples = opts.value("min_samples", 0);

    // Snapshot the aggregation (safe to read while the sampler runs; report is usually after
    // stop()).
    std::vector<Stack> stacks;
    unsigned long long total = 0;
    {
        std::lock_guard<std::mutex> lk(g_agg_mtx);
        stacks.reserve(g_agg.size());
        for (auto& kv : g_agg) {
            stacks.push_back(kv.second);
            total += kv.second.count;
        }
    }
    double wall = wall_seconds();
    if (total == 0) {
        return json{{"ok", false},
                    {"error",
                     "no samples captured — was a modeling op actually running while the "
                     "profiler was active? (in modeling mode only doc_* ops are sampled)"}};
    }

    ensure_sym();
    SymRefreshModuleList(GetCurrentProcess());

    std::unordered_map<DWORD64, std::string> cache;
    unsigned long long unresolved = 0;
    auto resolve = [&](DWORD64 a) -> const std::string& {
        auto it = cache.find(a);
        if (it != cache.end()) return it->second;
        std::string s = resolve_addr(a, &unresolved);
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
        for (DWORD64 a : st.addrs) labels.push_back(resolve(a));

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

    // module table (no top cap — there are only a handful of DLLs)
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
#endif
}

}  // namespace prof
}  // namespace aicad
