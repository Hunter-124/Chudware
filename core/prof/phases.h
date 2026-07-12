// Per-phase wall-clock timers for the simulation paths (mesher / FEA static / modal /
// thermal / fatigue / LBM / flow_run / overlay).
//
// WHY this exists: the sampling profiler (prof/sampler.h) answers "WHERE inside a solve did
// the CPU go" statistically, but the orchestrator optimizing simulation throughput needs a
// deterministic, always-on answer to "how long did mesh vs assemble vs factorize vs post
// take FOR THIS study". The env-gated FeaProf stderr printer (fea_static.cpp,
// AI_CAD_FEA_PROFILE=1) proved the value of the phase split but is (a) FEA-static only and
// (b) text-on-stderr, invisible to RPC callers. This module is the structured, always-on,
// engine-wide generalization: coarse RAII scopes record into an in-memory registry that the
// `prof_phases` RPC (and `prof_report`/`prof_stop`, additively) surface as JSON.
//
// DESIGN:
//   - A StudyScope opens one per-study record (domain = "fea_static", "lbm", "mesh_tet", ...)
//     on the CURRENT thread (thread-local stack, so nested studies — e.g. flow_run > lbm —
//     each get their own record). On destruction the completed record is appended to a
//     bounded global ring (newest kept) plus cumulative per-domain/phase totals.
//   - A PhaseScope records its wall time into the innermost open study on this thread;
//     with no study open it accumulates into the global "unscoped" totals instead.
//   - add() is the manual variant for scattered slices (e.g. per-checkpoint convergence
//     sampling inside the LBM loop) where a scope per slice would be noisy.
//
// OVERHEAD: two steady_clock::now() calls per scope and one mutex acquisition per COMPLETED
// STUDY (phases themselves are recorded lock-free into the thread-owned record). Scopes are
// deliberately coarse (per solver phase, never per element/iteration), so the cost is noise.
//
// THREADING: modeling/sim RPCs are serialized under the doc lock, but studies can also run
// on background job threads — the thread-local study stack keeps concurrent studies on
// different threads independent. Phases recorded on a thread with no open study (e.g. an
// OpenMP worker) fall into the "unscoped" bucket rather than corrupting another thread's
// study.
#pragma once

#include <chrono>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad {
namespace prof {
namespace phases {

// Open a per-study record for the current thread. `domain` must outlive the scope
// (string literals only — "fea_static", "lbm", ...).
struct StudyScope {
    explicit StudyScope(const char* domain);
    ~StudyScope();
    StudyScope(const StudyScope&) = delete;
    StudyScope& operator=(const StudyScope&) = delete;

private:
    void* rec_;  // opaque StudyRec*, owned until the dtor hands it to the registry
};

// Record the wall time of one named phase into the innermost open study on this thread
// (or the global unscoped totals when none is open). `name` must outlive the scope
// (string literals only).
struct PhaseScope {
    explicit PhaseScope(const char* name);
    ~PhaseScope();
    PhaseScope(const PhaseScope&) = delete;
    PhaseScope& operator=(const PhaseScope&) = delete;

private:
    const char* name_;
    std::chrono::steady_clock::time_point t0_;
};

// Manual accumulation (same routing as PhaseScope) for scattered slices, e.g. the
// convergence-sampling instants inside the LBM stepping loop.
void add(const char* name, double ms, unsigned long long count = 1);

// Completed studies since process start / last reset (cheap, for status lines).
unsigned long long completed_count();

// Structured report:
//   { "ok": true,
//     "completed": <n studies completed since reset>,
//     "kept": <n studies in the ring>,
//     "studies": [ {"domain", "seq", "total_ms",
//                   "phases": [{"name","ms","count","pct"}]}  // oldest -> newest
//                , ... ],
//     "totals": { "<domain>": {"runs", "total_ms",
//                              "phases": {"<name>": {"ms","count"}}}, ... },
//     "unscoped": { "<name>": {"ms","count"}, ... } }
// reset=true clears the ring + totals AFTER building the report.
nlohmann::json report(bool reset = false);

// Clear the ring + totals (call before a measured run).
void reset();

}  // namespace phases
}  // namespace prof
}  // namespace aicad
