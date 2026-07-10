# Profiler expansion for multi-subagent 3D modeling

_Draft plan — 2026-07-09. Topology decision: **one engine process, many subagents** (all
subagents share a single MCP/`libaicad` instance and its global `g_mtx`)._

## Problem, precisely

The heavy modeling in a subagent run happens in the **MCP server process**, not the GUI. Live
evidence (2026-07-09 run): pid 27387 (`mcp_server.server`, `services/dist/AI-CAD/libaicad.so`)
had ~1445 s of CPU on its **main thread**, all other threads idle — the textbook signature of
everything serialized under one lock.

Two structural blind spots make the current profiler unable to explain this run:

1. **No profiling path into the MCP engine.** `prof_start/prof_stop/prof_report` are called
   only from the GUI host (`host/src/ui/profiler_window.*`). `grep` finds zero callers in
   `services/`. The GUI profiler window drives the GUI's *own* `libaicad` instance — a
   different process with a different `g_mtx`. The only way to profile the MCP engine today is
   `AI_CAD_PROFILE=1` at launch, which needs a restart.

2. **The sampler is on-CPU only; the multi-subagent cost is off-CPU.** `core/prof/sampler.h`
   samples the single registered work thread. With N subagents funneling into one lock, the
   dominant cost is **time blocked waiting to acquire `g_mtx`** (queueing). A CPU sampler shows
   a clean flame graph of the one op holding the lock while everyone else stalls invisibly.

## Plan

### Item 1 — Runtime profiling hook in the MCP server (prerequisite for everything)
Expose the existing `prof_start/prof_stop/prof_report` RPCs through the MCP server so the engine
can be profiled at runtime without a restart. Two sub-parts:
- A thin MCP tool (e.g. `engine_profile {action: start|stop|report, hz?, mode?, top?}`) in
  `services/mcp/` that forwards to `aicad_call("prof_start"/...)` via the same core bridge the
  other tools use. Keep it out of the `lite` profile's dropped-tools list, or gate behind an env
  flag so it never clutters model tool lists in normal runs.
- Document the perturbation cost (sampling overhead) and make it opt-in per run. Do **not** fire
  it at a live production run without asking — start it on the *next* heavy run.

_Rationale: without this, the workload the user cares about is simply unobservable. This is the
single highest-leverage change and unblocks measuring items 2–4 instead of guessing._

### Item 2 — Contention / wait-time visibility on `g_mtx` (the actual bottleneck)
Instrument the one choke point: `DocLock` in `core/doc/session.cpp` (~line 204), which every op
batch passes through and which already tracks owner/held state. Add, cheaply:
- **Wait duration**: `now()` before `g_mtx.lock()`, delta after acquire → accumulate per caller.
- **Hold duration**: acquire→release delta.
- **Queue depth**: an atomic counter incremented before lock / decremented after acquire.
- Aggregate into a new report section (peak/mean wait, %time-blocked, contended-caller table),
  surfaced via a `prof_contention` RPC (or folded into `prof_report`).

This is what turns "the flame graph looks fine but subagents are slow" into a number:
*% of wall-clock spent blocked on the lock*. It also gives the go/no-go signal for item 3.

_Keep it near-zero-overhead when profiling is off: a single `if (contention_enabled)` around the
timestamp captures, or a compile-time/atomic gate._

### Item 3 — Architectural fix (only if item 2 confirms serialization dominates)
If measured lock-wait is the dominant cost, the profiler expansion has done its job; the fix is
architectural and beyond the profiler:
- **Per-doc locking**: replace the single global `g_mtx` with a per-session lock so subagents
  editing *different* docs run truly concurrently. This is the real unlock for "many subagents,
  one engine." Requires auditing every cross-doc path (copy-between-docs at session.cpp:2434,
  world-bodies, etc.) for shared state that still needs a global lock. Non-trivial — scope as
  its own task after measurement.
- Fallback if per-doc locking is too invasive: keep one engine but document that subagent
  modeling is latency-bound and size fleets accordingly.

### Item 4 — Cheap independent win: OCCT parallelism
Independent of the lock story. Today only `core/geom/thread.cpp:179` sets
`SetRunParallel(true)`; general booleans/tessellation and the STEP reader
(`core/doc/formats.cpp:1535` `SetParallel(False)`) run single-threaded, leaving cores idle even
for one op. Evaluate enabling `SetRunParallel(true)` / `OSD_ThreadPool` on the hot boolean and
tessellation paths. Measure with item 1's profiler before/after. Note: this can *worsen* the
multi-subagent case (each op grabbing all cores while holding the global lock) — so land it
*with* per-doc awareness, and measure.

## Sequencing
1. Item 1 (MCP profiling hook) — unblocks all measurement, touches only `services/`.
2. Item 2 (contention metrics) — small, surgical core change at the `DocLock` seam.
3. Run the next heavy subagent workflow with both on; read the % blocked number.
4. Branch: if lock-bound → item 3 (per-doc locking). If a single op is CPU-bound → item 4.

## Non-goals
- Prettier flame graphs / more sampling modes — not the bottleneck for this scenario.
- Touching the currently-live run. All changes take effect on the *next* engine start.
