# Performance work — overnight session results (2026-07-09)

Implements the approved plan in `~/.claude/plans/the-way-were-handling-proud-sketch.md`.
All changes are in the working tree (uncommitted, per standing instruction). A backup of the
core/service diffs is in `docs/perf_night/phase1_wip.patch`.

## TL;DR — what shipped and what it bought

| Change | Status | Measured effect |
|---|---|---|
| **Parallel OCCT booleans** | ✅ shipped + verified | **boolean −31%, pattern −32%, heavy_mix −38%, cold-open −19%** |
| **tcmalloc auto-preload (MCP)** | ✅ shipped + verified | **boolean −10%, pattern −10%** (allocator swap, no logic change) |
| MCP `engine_profile` tool | ✅ shipped | profiles the MCP engine at runtime (was impossible) |
| `g_mtx` contention meters | ✅ shipped + verified | new `prof_contention` RPC; per-caller lock wait/hold/queue |
| Per-feature rebuild timing | ✅ shipped + verified | `feature_status[].ms` now real (was hardcoded 0) |
| CI perf-regression harness | ✅ shipped | `tools/perf_regression.py` + baseline + gate stage |
| LTO/IPO build option | ✅ shipped (opt-in) | speed −1..4% (modest), **binary 25.9→11.7 MB** |
| **Persisted geometry cache (2b)** | ✅ shipped + verified | **cold-open 174→9 ms (~17×)**; exact geometry parity |
| **Handoff double-build (2a)** | ✅ solved via 2b | agent-published part opens on host in **1.1 ms, no re-tessellation** |
| Per-doc locking (Phase 3) | 📐 designed, not built | see Design §C (only if shared-engine) |
| Full live co-edit (§D) | 📐 designed | shared-engine architecture; needs your design decision |

## Cumulative (vs the original glibc + serial-boolean engine)
```
boolean    85.1 → 54.4 ms   -36%
pattern   179.3 → 107.9 ms  -40%
heavy_mix 116.5 → 69.8 ms   -40%
open_cold 173.8 →  9.2 ms   -95%  (19x, persisted cache)
```

## The single most important correction

My initial reading — "serialization on `g_mtx` dominates" — was **wrong for the deployment as
it runs today.** The 486% `pct_wall_blocked` number is from a *synthetic 6-thread test* that
simulates the intended "one engine, many subagents" topology. But production runs subagents as
**separate MCP processes, each with its own `libaicad` and its own `g_mtx`** — separate
processes don't contend on a shared lock. The live snapshot confirmed it: one MCP process had
~1445s pinned to its **main thread**, all worker threads idle.

So the real current bottleneck is **single-threaded per-op OCCT compute** (booleans dominate),
not lock contention. That's why the highest-value change turned out to be parallelizing the
booleans, not per-doc locking. The contention meter is still valuable — it's the instrument that
will prove/disprove lock-boundness **if/when** you move to the shared-engine topology you
described ("hasn't been fully provisioned yet").

## Measured numbers (this machine, `tools/perf_regression.py --iters 9`)

Median ms per full build (doc_new + apply_ops + close), before → after parallel booleans:

```
boolean (24-hole cut)   85.1 → 59.0   (-31%)
pattern (48-hole cut)  179.3 → 121.2  (-32%)
heavy_mix              116.5 →  72.4  (-38%)
open_cold (rebuild)    173.8 → 140.6  (-19%)   ← re-runs the boolean-heavy tree on open
bolt_circle feature     84.4 →  43.8 ms (per-feature timing)
```

Cold-open of a modest 3-feature part is **~140 ms and scales with feature count** — this is the
number that justifies the persisted-geometry cache (Design §B). It's real, not hypothetical.

## Files changed
- `core/geom/booleans.cpp` — parallel BOP (deferred `SetArguments`/`SetTools` +
  `SetRunParallel(true)` on cut/fuse/common/cut_many/fuse_many).
- `core/doc/session.cpp` / `session.h` — `DocLock` contention meters + `contention_report()` +
  `set_current_rpc_method()`.
- `core/doc/document.cpp` / `document.h` — per-feature compile timing (`FeatureStatus::ms`).
- `core/api/libaicad.cpp` — `prof_contention` RPC + per-call method stamping.
- `services/mcp/cad_tools.py` — `engine_profile` MCP tool.
- `core/CMakeLists.txt` — `AICAD_LTO` option (opt-in, CMP0069 NEW).
- `scripts/gate.sh` — `stage_perf` (env-gated `AICAD_RUN_PERF=1`).
- `tools/perf_regression.py`, `tools/perf_baseline.json` — new harness + baseline.

## Correctness
- All **54/55** core ctests pass. The 1 failure (`sweep`, a helix guide-curve *volume* check)
  is **pre-existing** — confirmed by reverting `booleans.cpp` to HEAD and re-running: it still
  fails. Not caused by the parallel-boolean change. Worth a separate look (unrelated to perf).
- Metering overhead is negligible: `DocLock` is taken **once per RPC** (82 call-sites, all
  function-scope, none in loops), so it's ~2 `steady_clock` reads + a few relaxed atomics per
  multi-ms RPC (<0.01%). Left always-on deliberately.

## Activation (IMPORTANT — the new engine is not deployed)
The changes are built in `core/build-occt-linux/bin/libaicad.so` but **not** staged to
`services/dist/AI-CAD/` (the live MCP/GUI processes have the old `.so` memory-mapped; overwriting
mid-run is avoided). The running MCP servers also imported the old Python. To use any of this:
1. Rebuild is already done: `core/build-occt-linux/bin/libaicad.so`.
2. Stage it: copy to `services/dist/AI-CAD/libaicad.so` (or set `AI_CAD_LIBAICAD` to the build
   path) **after** the current live sessions finish.
3. Restart the MCP servers so they load the new `.so` + new `cad_tools.py`.
4. Then `engine_profile start` / `engine_profile contention` work against real runs, and the
   parallel-boolean speedup is live.

## Fast allocator (tcmalloc)
OCCT booleans/meshing are allocation-heavy; tcmalloc beats glibc malloc by ~5-10% on the
modeling battery (measured, ctest still green under it). The engine is dlopened via ctypes, so
linking an allocator into libaicad can't override the already-resolved glibc malloc — `LD_PRELOAD`
before first allocation is the only reliable path. `services/mcp/server.py::main()` therefore
self-reexecs once with `LD_PRELOAD=libtcmalloc_minimal` (loop-guarded, opt-out
`AICAD_NO_FAST_MALLOC=1`), so every MCP/subagent server start gets it automatically — verified the
allocator is mapped in after reexec. **Desktop app** (host is the main binary, not a dlopen)
should link tcmalloc directly — a follow-up (compile-only change; couldn't runtime-verify the GUI
tonight). The perf baseline is kept allocator-neutral (glibc) so it measures engine changes, not
the allocator; tcmalloc is a deployment multiplier on top.

## LTO
Opt-in: `cmake -S core -B <dir> -DAICAD_LTO=ON -DCMAKE_BUILD_TYPE=Release -DAICAD_WITH_OCCT=ON`.
Measured marginal speed win is small (−1..4%; ~0% on boolean-dominated ops) because the hot path
lives inside prebuilt OCCT, which LTO can't touch — it only trims our own code. Binary shrinks
25.9→11.7 MB. Kept OFF by default (link-time cost isn't worth it for dev/gate builds; enable for
shipped release artifacts if the size matters).

---

# Design specs for the deferred (higher-risk) items

§C and §D were intentionally **not** implemented — each is a correctness-sensitive architectural
change that needs review, not a blind overnight commit.

## §A / §B — SHIPPED (persisted geometry cache; handoff double-build solved)
Implemented + verified. On `doc_save`, the compiled tessellation + ops-response are embedded as
additive `cache.aicm` / `cache.json` entries in the `.step+` (via the existing `extra` map — old
readers ignore them, `document.json` is untouched). On `doc_open`, if the cache's engine key
(OCCT version + `aicad-geomcache/1` schema) and the FNV-1a hash of the canonical `document.json`
(minus the volatile `id`) both match, `doc_open` returns the cached response + mesh and **skips
`compile_document` entirely**; `Session::compiled` is left empty so the first edit/pick recompiles
from features (the parametric source of truth). Files: `stepplus.{h,cpp}`
(`stepplus_doc_hash`/`stepplus_cache_engine_key`), `session.cpp` (`doc_save` write, `doc_open`
fast path). Verified: cache hit 174→9 ms; **exact** volume parity vs fresh rebuild; edit
recompiles correctly; hash-mismatch + engine-key-mismatch both fall back to rebuild.
**Handoff (2a) falls out for free:** the agent→GUI handoff publishes via `doc_save` and the host
reads via `doc_open`, so the host cache-hits the agent's mesh (measured 1.1 ms, no
re-tessellation). Full live *co-edit* (host editing an agent's in-progress part with `owner_map`)
still needs §D.
**Bump `aicad-geomcache/1` in `stepplus_cache_engine_key()` on any geometry-compiler change that
could alter output** — that's the one maintenance rule.

## §C — Per-doc locking (only if you go shared-engine)
Replace the single global `g_mtx` with per-`Session` locks so subagents editing *different* docs
run concurrently. **Do not implement without review + ThreadSanitizer.** Hazards:
- `g_sessions` (the `doc_id→Session` map) needs its **own** lock, separate from per-doc locks, or
  insert-while-iterate is UB.
- Cross-doc paths need lock-ordering discipline or they deadlock: copy-between-docs
  (`session.cpp:2434`), assembly components referencing part docs, `dep_notify` (note the existing
  `dep_notify_unlocked` variant — the lock discipline is already delicate).
- Crash-recovery (`g_mtx_held`/`g_mtx_owner`/`force_release_doc_lock`) assumes exactly one lock.
Gate this on a **real** `prof_contention` measurement from a shared-engine run showing
`pct_wall_blocked` actually high — today it isn't (separate processes).

## §D — Full live co-edit (north star)
Model lives in the window; agent + user both edit without interrupting each other. Requires the
GUI and the agent's MCP process to drive **one shared engine/document** with op-streaming +
conflict handling (echoes prior PLM "T-R8 live co-edit"). This is the architectural convergence
point: shared-engine makes §C (contention) and co-edit the *same* problem. Design spike, gated on
the topology decision.

## Tried and rejected (measured, then reverted)
- **Cross-rebuild tessellation cache** (global map keyed by exact serialized-BREP hash in
  `tessellate_body`). Realistic benefit only ~9% on incremental builds — the harness's −43%/−48%
  was an artifact of rebuilding byte-identical parts (artificial cache hits). Against that: a real
  memory-blowup risk (capped by entry count, but one body's mesh is MBs → GBs possible),
  per-unique-body `BRepTools::Write` overhead that would slow all-unique workloads, and perf-
  baseline contamination. Not a clean win → reverted. If revisited: byte-bounded LRU, key on the
  feature sub-tree (not a full BREP re-serialize), baseline measured on cold (all-miss) builds.
- Next safe candidate: the same per-body tessellation reuse done *right* (subtree key, bounded).
  Also: link tcmalloc into the desktop host binary (works there — host is the main binary).
