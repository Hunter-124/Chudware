# Performance audit — modeling core, sketcher, GUI freezes, program bloat (2026-07-10)

Measured on the live machine + 4 parallel code audits. Every finding below was verified in
code at the cited lines or measured directly; nothing is folklore.

## 0. Measured ground truth (read this first)

Workload: 100×80×40 box + 3 mm all-edge fillet + 16-hole bolt circle (profile_modeling "all").

| Path | Wall time | Notes |
|---|---|---|
| Kernel, in-process (`AI_CAD_ENGINE_DAEMON=0`) | **0.10 s** | phase_ms: features ~53 ms, tessellation ~29 ms |
| Same call, daemon mode with hostd unreachable | **15.34 s** | constant across 1/2/12 CPUs — it's a timeout, not compute |
| profile_modeling.py full script (3 RPCs) | ~46 s | 3 × 15.3 s hostd penalty |

**The OCCT modeling kernel is fast.** The user-felt slowness has three layers, in order of
severity: (1) a 15 s-per-call hostd transport tax on the MCP/tools path, (2) synchronous
engine RPCs on the GUI render thread (freezes), (3) real but second-order kernel/sketcher
inefficiencies that matter as models grow.

## 1. P0 — hostd transport tax (the "crazy" part)

Symptoms measured live:
- `services/mcp/core_bridge.py:334-348` — `call()` retries `_call_via_daemon()` on EVERY
  call; `_DAEMON_FALLBACK_WARNED` only suppresses the warning, not the retry. When hostd is
  unreachable, **every engine call pays the full `connect(budget_s=15.0)`**
  (`services/mcp/daemon_bridge.py:303`) before falling back. 150x slowdown measured.
- The hostd rendezvous is racy: `/run/user/1000/aicad/` had `hostd-<key>.lock` present with
  **no matching `.sock`** for minutes; clients get ENOENT → spawn a replacement → the
  replacement loses the lock to the idle incumbent and exits → client burns the 15 s budget.
  **12 zombie `aicad-hostd` processes** observed, parented to MCP server processes; one
  incumbent hostd had 0 CPU time after 13 min (nobody ever reached it). Later a fresh hostd
  bound the socket successfully — so the failure is intermittent, matching the "sometimes
  fine, sometimes crazy slow" feel.
- `aicad-hostd` exists only in `core/build-occt-linux/bin` + `core/build-linux/bin`; it is
  NOT staged into `services/dist/AI-CAD/`, so processes resolving the dist candidate order
  can't even spawn it (Phase 7 packaging gap). Instance key = fnv(binary realpath), so
  GUI/MCP resolving different binaries silently get DIFFERENT engines (no doc sharing).

Fixes (in order):
1. **Sticky fallback** (S): in `core_bridge.call()`, skip the daemon once fallen back
   (`active_daemon_mode()` already exists for exactly this); optionally re-probe with
   `budget_s≈0.5, spawn=False` at most every 60 s.
2. **Fast-fail rendezvous** (S/M): treat lock-held-but-no-sock as "incumbent broken":
   bounded connect budget (~2 s), health-check the incumbent via the lock owner pid, reap
   zombies (`waitpid` on spawn loser), clean stale pytest/smoke socks+locks.
3. **Stage hostd into dist** (Phase 7 item) + single canonical instance key so GUI and MCP
   share one engine.
4. **Surface the fallback in the GUI/MCP status** — this failure was silent everywhere.

## 2. P0.5 — the built-in profiler is blind on Linux

`tools/profile_modeling.py` (in-process sampler): requested 1000 Hz × 10 s → got **76
samples** (effective ~1.7 Hz), most frames unresolved (`aicad!0x…`). It cannot see any of
the above. Also the machine itself was oversubscribed during measurement (qemu TCG VM at
375 % + rust builds + GUI rendering while idle) — load 8.3/12 cores; keep that in mind for
any benchmark run on this box. Fix the sampler cadence + symbolization (dist .so has no
symtab for `nm`; keep a debuginfo sidecar or use `perf` + `--call-graph dwarf`, which worked).

## 3. GUI freezes + always-show-a-preview architecture (agent audit)

The async seam exists (`core::AsyncRpc`, drained per-frame) but only ~6 call sites use it;
~200 engine round-trips still block the GL thread. Worse, **daemon mode silently removed all
timeouts**: `WorkerClient::call(timeoutMs)` reroutes at `worker_client.cpp:780-783` into
`aicad_ipc::EngineClient::call()` which blocks forever (`readLine(line, -1)`,
`ipc_client.cpp:191`) — GUI RPCs queue unbounded behind any heavy MCP/agent job on the
global `g_mtx`.

Freeze inventory highlights (GL thread, worst-case unbounded in daemon mode):
- Per-mouse-sample sync RPCs: sketch drag (`sketch_mode.cpp:6095/2754`), 3D-sketch drag
  (`sketch3d_mode.cpp:866`), push-pull (`viewport_panel.cpp:1394` — first sample = full
  rebuild), assembly drag (`:1487`), explode slider (`asm_panels.cpp:1055`, no debounce).
- PM live preview: sync `applyOps` + mesh reload per preview (`property_manager.cpp:2106`).
- Every-frame daemon event handlers do sync RPCs (`engine_events.cpp:62/75/111/139/230/234`),
  including full `doc_rebuild`s; bootstrap retries a sync subscribe+doc_list EVERY FRAME
  while the daemon is loading (violates the probes-back-off rule).
- `loadDocMesh` (disk read + parse + pick-cache build, `app_state.cpp:722-926`) runs on the
  GL thread at ~25 call sites (100 ms–2 s on big docs).
- File open/save/export, tab-switch fallback rebuild, sim_mesh (180 s!), drawing HLR ops —
  all sync on the GL thread.
- Two subsystems already do it right: post-agent `doc_rebuild` (app.cpp:2137-2180, async +
  doc-id generation guard + old mesh + "Refreshing model…") and sketch ghosts
  (`ghostsGen_`). Target = make everything look like those.

Numbered plan (suggested order P1→P6→P3→P4→P5→P2→P7→P8→P10→P9):
- **P1 (S)**: plumb `timeoutMs` through `EngineClient::call`/`engine::call` →
  `readLine(line, timeoutMs)`; on timeout return transport-error → toast. Converts
  "hung forever" into "bounded failure".
- **P3 (M)**: `DocMirror::applyOpsAsync` + `mutationGen_` counter; convert PM preview
  (single-in-flight, latest-wins), feature tree ops (batch N ops into one applyOps),
  equations/design table/config/asm panels, undo/redo.
- **P4 (M)**: all four drag pipelines → single-in-flight sample + `pendingTarget`
  overwrite; viewport renders last solved state (stale-but-live preview).
- **P5 (M)**: mesh loader worker thread (disk+parse+pick-cache off GL thread; GL does only
  buffer upload); `meshGen` drops stale completions; keep-last-good falls out free.
- **P6 (S)**: `rebuildsInFlight` counter → "Rebuilding…" corner chip + dimmed overlays.
- **P2 (M)**: engineEventsTick handlers → AsyncRpc; bootstrap retry ≤ every 500 ms.
- **P7/P8**: async tab switch fallback, file open/save, sim_mesh, drawing ops.
- **P9 (L, LAST)**: per-doc engine locking — improves latency, NOT required for GUI
  responsiveness once P1-P8 land.
- **P10 (S)**: cache Recent-file `fs::exists` (`file_explorer.cpp:406-421`).

## 4. Modeling core (agent audit — real but second-order today)

Verified working: incremental prefix cache (CompileCache, document.cpp:1570-1662), body
tess cache (7016-7061), parallel booleans (booleans.cpp:34/67/82), BRepMesh parallel flag.
Verified problems:
- **[P1] Cache bypass on the hot edit path**: `refresh_sketch_external_refs`
  (session.cpp:10294), `resolve_sketch_plane_doc` (:3394), `refresh_sketch3d_external_refs`
  (:10380) each run FULL UNCACHED `compile_document` inside every non-readonly apply_ops on
  affected docs (1-3 extra full rebuilds per edit). Fix: thread `s.compile_cache` through;
  read plane from warm `s.compiled`. Effort S.
- **[P1] Checkpoint capture O(N²)** with deep JSON/blob copies (document.cpp:1635-1658);
  import_cache keyed BY the embedded_brep blob string (1547-1553). Fix: hash keys,
  `shared_ptr<const FeatureResult>` sharing. Effort M.
- **[P2] Fixed absolute tess deflection 0.05 mm** (document.cpp:7082) — not size-relative;
  huge parts mesh at millions of tris. Fix: bbox-diagonal-relative deflection. Effort S.
- **[P2] Owner map recomputed over the whole feature history every compile**
  (document.cpp:7143-7215) even when features were reused. Fix: fold owners into
  CompileCkpt. Effort M.
- **[P2] Per-edit whole-doc copies**: `json working = s.doc`, `pre_doc`, undo dump,
  `resp["tree"]`, chain re-dump of every feature (1576) — megabytes per keystroke on
  import-heavy docs. Fix: out-of-line content-addressed blob store for embedded_brep.
- **[P2] Cache hits deep-copy the payload** (CompileResult/AicmBody copies,
  document.cpp:1585/7065/7119). Fix: shared_ptr/COW buffers.
- **[P2] Booleans lack `SetNonDestructive(true)`** while caches share live TShapes
  (correctness-adjacent; cheap insurance).
- **[P3]**: fillet drop-failing fallback is O(E²) full rebuilds (dress.cpp:142-163 — bisect
  instead); per-feature `signed_vol` Gauss integration (document.cpp:1497-1535);
  `unify_same_domain` after every fuse; AICM temp-file round-trip per rebuild (shared-mem
  handoff once hostd owns both ends).

## 5. Sketcher (agent audit)

Architecture: LM over analytic DENSE Jacobian (Eigen), one monolithic System per sketch, no
cluster decomposition; JacobiSVD min-norm step ALWAYS (nsolve.cpp:335, O(n³) worst constant).
- **[P1] Drag rebuilds the entire System + runs ColPivQR `analyze` + full-V JacobiSVD
  statuses EVERY mouse-move frame** (sketch.cpp:2641-2764, 2744, 2087; registration
  re-probes every residual, nsolve.cpp:156-159). Fix: persist System in SketchState for the
  drag; skip analyze/statuses during drag, run once on release. Effort M.
- **[P1] Auto-coincidence on every sketch-mutating batch**: O(E²·C) endpoint scan + 1 cold
  full solve + 1 MORE cold solve PER proposal with params deep copy
  (session.cpp:3226-3234, 9536-9563, 9633-9700). Fix: scope scan to touched entities, hash
  the coincident set, warm-start trials. Effort M.
- **[P1] Drag is a sync blocking RPC on the GUI thread per frame** (sketch_mode.cpp:6095 →
  worker.call, engine side under g_mtx). Fix: AsyncRpc latest-wins (see GUI P4).
- **[P2]**: JacobiSVD per damping trial (up to 12×/iter; LDLT-first gated to interactive
  mode, keep SVD for oracle parity); deadline only checked between outer iterations;
  `dependent_rows` = repeated full QRs, guaranteed triggered because redundant coincidents
  are deliberately kept (9629-9631); spline tangency numeric-scan = 625-pt scan × central
  differences per evaluation (sketch.cpp:571-620 — warm-start (p,q) params).
- **[L] Connected-component decomposition** — biggest asymptotic win for large sketches;
  drag solves only the dragged component.
- Python-oracle parity is an explicit invariant: gate every relaxation behind an
  interactive-mode flag so the differential harness keeps the exact path.

## 6. Program-wide bloat (agent audit)

- **12x autosave stacking: root-caused.** Legacy mode only: (a) `_tick_autosave` re-arms
  unconditionally and `stop` can't cancel an in-flight chain (cad_tools.py:745-787 — no
  generation token); (b) dual module identity `mcp_server.cad_tools` vs
  `services.mcp.cad_tools` (imported by piping_tools.py:421) = two independent timer slots.
  Daemon mode is clean (single cv thread, dirty+revision skip). Fix: generation counter +
  kill the import alias. Effort S.
- **[P1] Parts live-search fan-out threads uncancellable** (gui_rpc.py:364-417 — `cancelled`
  never set True; evicted jobs keep scraping, holding per-site rate-limit locks that delay
  the user's CURRENT search). Effort S.
- **[P2]** GUI renders at full vsync rate when idle (app.cpp:1906, no
  `glfwWaitEventsTimeout`); CEF boots unconditionally at launch (main.cpp:378-385);
  engine-events bootstrap sync-RPCs every frame during engine load (engine_events.cpp:250);
  parts thumbnail GL cache unbounded (parts_internal.h:191 — no eviction, no
  glDeleteTextures).
- **[P3]** vendors health = ~20 serial live scrapes (health.py:140); scrape queue single
  worker (queue.py:219); hostd child supervisor wakes 5×/s with zero children
  (child_supervisor.cpp:120); preflight_events.jsonl unbounded append at repo root;
  `DaemonClient._events` uncapped (latent).
- Verified clean: hostd autosave/doc-registry events, ECAD autosave, parts/PLM GUI poll
  throttles, CEF pump idle path, legacy handoff poller gating.

## 7. Recommended execution order

1. **hostd transport (P0)**: sticky fallback one-liner, fast-fail rendezvous, stage binary
   into dist, zombie reaping. Biggest single win; mostly S effort.
2. **GUI P1 (daemon timeouts) + P6 (rebuilding chip)** — bounded failures + honest UX.
3. **Sketcher drag trio**: persistent System + diagnostics-on-release (engine), async
   latest-wins drag (GUI P4), auto-coincide fix. This is "the sketcher feels instant".
4. **GUI P3/P5**: applyOpsAsync conversions + mesh load off the GL thread. This is "the
   modeler never freezes, always shows last-good preview".
5. **Modeling core P1s**: external-ref cache bypass + checkpoint copy amplification.
6. Bloat S-items opportunistically (autosave token, live-search cancel, bootstrap backoff,
   idle render throttle, thumbnail LRU).
7. **Per-doc locking last** — scales the fleet, not needed for GUI responsiveness.

Re-baseline `tools/perf_regression.py` only after (1), in legacy AND daemon modes, on a
quiet machine (the current baseline numbers are polluted by the 15 s tax).
