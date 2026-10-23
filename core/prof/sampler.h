// In-process sampling profiler for the Chudware core.
//
// WHY this exists: modeling slowness (extrude/fillet/boolean/pattern/tessellate/rebuild)
// lives mostly DEEP INSIDE OCCT and our sketch solver, where a per-feature stopwatch can
// only say "fillet took 2 s" — never *where inside* the 2 s went. A sampling profiler
// periodically captures the call stack of the thread doing the work and aggregates the
// samples, so the hot frames rise to the top statistically. Because the core runs every
// modeling op synchronously on one thread under a single global mutex (see
// core/doc/session.cpp g_mtx), sampling ONE registered "work thread" is both sufficient
// and correct.
//
// DESIGN (the two hard parts of an in-process sampler, and how we dodge them):
//   1. Heap-lock deadlock. SuspendThread can freeze the work thread mid-malloc while it
//      holds the CRT heap lock. If the sampler then allocated (e.g. via DbgHelp) it would
//      block forever. So while a thread is suspended we do ZERO allocation and call NO
//      DbgHelp: the stack is walked with RtlLookupFunctionEntry + RtlVirtualUnwind, which
//      read the module's memory-mapped .pdata unwind info lock-free. We capture only raw
//      return addresses.
//   2. Symbolization cost/locking. Turning addresses into module!symbol(file:line) is done
//      LATER, in report(), when nothing is suspended, via DbgHelp (Sym*). This is the only
//      place DbgHelp is touched, so its single-threaded lock is never contended.
//
// Symbols: libchudware.dll ships a PDB (see CMakeLists /Zi /DEBUG) so OUR frames resolve to
// real names. OCCT prebuilt has no PDBs, so its frames resolve to module-level
// "TKBO!0x1234" — which is exactly the attribution we want there ("70% of time in TKBO =
// booleans dominate").
//
// This header and the public API are cross-platform. There are two real capture backends:
// Windows (SuspendThread + RtlVirtualUnwind + DbgHelp, above) and Linux (a periodic signal
// delivered to the registered work thread, an async-signal-safe backtrace() into a lock-free
// ring, and dladdr + __cxa_demangle symbolization in report()). Both feed ONE platform-neutral
// aggregation + report core. On any OTHER platform every entry point is a no-op and report()
// returns an "unsupported" envelope.
#pragma once

#include <string>
#include <nlohmann/json.hpp>

namespace chudware {
namespace prof {

// Start the background sampler. Idempotent: a second start() while already running just
// updates nothing and returns false. interval_us is the sampling period (clamped to a sane
// floor); mode is informational ("modeling" | "all") and recorded in the report — the
// actual scoping of WHICH thread/when is driven by the TargetScope guards placed by the
// caller (see core/api/libchudware.cpp). Returns true if this call started the sampler.
bool start(unsigned interval_us, const std::string& mode);

// Stop the background sampler and join its thread. Accumulated samples are RETAINED so a
// subsequent report() (or a final report on shutdown) can still symbolize them. Idempotent.
void stop();

bool active();
unsigned long long sample_count();
unsigned interval_us();
std::string mode();

// Register the CURRENT thread as the sample target (ref-counted, so nested scopes are
// safe). While a target is registered the sampler periodically suspends it and records its
// stack. clear_target() unregisters (closing the duplicated handle at depth 0). Prefer the
// RAII TargetScope below.
void set_target();
void clear_target();

// RAII wrapper: TargetScope(true) registers the current thread for its lifetime. Construct
// with engage=false to make it a no-op (lets the caller decide per-method without branching
// around the object).
struct TargetScope {
    bool engaged_;
    explicit TargetScope(bool engage) : engaged_(engage) {
        if (engaged_) set_target();
    }
    ~TargetScope() {
        if (engaged_) clear_target();
    }
    TargetScope(const TargetScope&) = delete;
    TargetScope& operator=(const TargetScope&) = delete;
};

// Symbolize the accumulated samples and render reports. opts (all optional):
//   { "top": int (default 40),            top-N rows in the flat tables
//     "write_folded": "path",             write Brendan-Gregg folded stacks to this file
//     "write_speedscope": "path",         write a speedscope.app JSON profile
//     "min_samples": int }                drop frames below this self-sample count from flat
// Returns:
//   { "ok": true,
//     "summary": {samples, unique_stacks, interval_us, mode, wall_seconds, unresolved_frames},
//     "modules": [{module, self, pct}],   self-time by DLL — the fastest hotspot read
//     "flat_self":  [{frame, self, pct}], hottest leaf frames (where the CPU actually is)
//     "flat_total": [{frame, total, pct}],frames most often ON the stack (callers included)
//     "folded": "a;b;c 12\n...",          full folded text (also returned inline)
//     "wrote": ["path", ...] }
// On an unsupported platform, or when no samples were captured, returns {"ok":false,"error":...}.
// The folded / speedscope / flat shapes are byte-stable; nested-tree views live in tree_report().
nlohmann::json report(const nlohmann::json& opts);

// Nested call-tree view over the current aggregation (ADDITIVE).
// Symbolizes via report(), then builds a hierarchical tree from the folded stacks
// (see prof/tree.h). opts (all optional):
//   { "path": "a/b/c",   // drill into a subtree (empty = whole tree)
//     "depth": int,      // -1 unlimited; 0 = selected node only; 1 = +direct children
//     "top": int }       // max children per level (0 = unlimited); hot-first
// Returns tree::to_json envelope, plus "summary" from the underlying report when ok.
// Does NOT write folded/speedscope files (pass those to report() if needed).
nlohmann::json tree_report(const nlohmann::json& opts = nlohmann::json::object());

}  // namespace prof
}  // namespace chudware
