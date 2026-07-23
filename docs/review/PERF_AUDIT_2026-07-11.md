# Performance re-audit — post-T-PERF hotspots + doc_open zip-decode fix (2026-07-11)

Follow-up to the 2026-07-10 performance audit (since pruned; its findings shipped as the
T-PERF1..10 wave). Every
number here is **fresh**, measured with the (now-working) in-process sampling profiler on
2026-07-11. It supersedes the pre-fix hotspot ranking in the earlier audit.

> **Measurement discipline.** The box was under real load (~16 on 12 cores — other agents
> modeling live) so *absolute* ms are inflated, but self-time **%** ranking within one op is
> stable across runs. All runs were **in-process** (`CHUDWARE_ENGINE_DAEMON=0`), a separate
> process that COW-maps the dist `.so` and contacts **no daemon** — it never touched the live
> hostd/MCP engines. Profiler cadence + symbolization now work (T-PERF10 fix): e.g. the `open`
> run got 3868 samples / 14 unresolved, vs the audit's "76 of 10k / all unresolved".
>
> Reproduce (safe, in-process):
> ```
> CHUDWARE_ENGINE_DAEMON=0 python3 tools/profile_modeling.py --workload open --seconds 4 --hz 999
> CHUDWARE_ENGINE_DAEMON=0 python3 tools/profile_modeling.py --workload all  --seconds 5 --hz 999
> ```

## 0. The profiler works now — use it, not the old audit's numbers

Confirmed working on Linux (the audit's "blind on Linux" predates the T-PERF10 sampler
rewrite: absolute-time `clock_nanosleep` cadence + ucontext leaf frame + ELF `.symtab` walk).
Two constraints when profiling on this machine:
1. **Always `CHUDWARE_ENGINE_DAEMON=0`.** `core_bridge` defaults to daemon mode; without the
   opt-out the profiler routes into the **live shared engine** the other agents are using.
2. Point at a freshly-built engine with `CHUDWARE_LIBCHUDWARE=/abs/path/libchudware.so` to profile a
   change without staging over the live dist `.so`.

## 1. SHIPPED — doc_open was 81% zip CRC32 + inflate → now memmove-bound (~5.2×)

**The finding.** `open` workload (build a 3-feature part, save, reopen from the persisted
geometry cache — this is the **agent→GUI handoff path**, hit every time an agent publishes a
part):

| frame | before | after |
|---|---:|---:|
| `chudware!mz_crc32` | **47.3%** | 0.5% |
| `chudware!tinfl_decompress` | **33.8%** | 1.0% |
| `c!__memmove_avx512` | 13.8% | 75.2% (now the floor) |
| **throughput** | **85 opens/s** | **440 opens/s (~5.2×)** |

**Root cause.** The §B persisted-geometry cache stores the compiled mesh as `cache.aicm`
inside the `.step+` zip. It was written **DEFLATE** and read with miniz flags=0, so every
open decompressed (`tinfl`) *and* CRC-checked (`mz_crc32`, miniz's slow byte-wise CRC) the
multi-MB mesh blob — before the cache was even validated. The cache is already integrity-gated
at a higher level (engine-key + FNV doc-hash) and is display-only (first edit recompiles from
features), so the zip CRC was redundant belt-and-suspenders.

**The fix** (`core/doc/stepplus.cpp`, 2 edits):
- **Write** `cache.aicm` **STORED** (not deflated) — it's large binary that barely compresses.
- **Read** STORED entries (`m_method == 0`) with `MZ_ZIP_FLAG_COMPRESSED_DATA` — for a STORED
  entry the "compressed" bytes *are* the data, so miniz skips **both** the inflate and the
  CRC32 (miniz.c:4532–4541: the no-decompress branch only CRCs when the flag is clear).

Net: open drops to a raw `memmove` of the mesh out of the zip. The remaining 75% memmove is
the floor; a future zero-copy (mmap the STORED entry, parse AICM in place) would shave it.

**Correctness — proven from six angles:**
- `cache.aicm` is STORED and byte-identical (261142 == 261142, uncompressed).
- `cache_hit: True` on reopen (engine-key + doc-hash validated by session.cpp).
- AICM parses + renders 1 body across 1760 opens, zero errors; body metadata identical to a
  fresh from-features rebuild.
- `import_roundtrip` ctest passes.
- Python stdlib `zipfile` reads the STORED entry (any zip reader does) → fully back/forward
  compatible; old/other readers are unaffected (they read `document.json`, not the cache).
- Only cost is a modestly larger `.step+` (mesh no longer deflated); for the transient handoff
  files this is free, and the mesh was already being stored either way.

**Deploy note:** built + verified in `core/build-linux/bin/libchudware.so`. **NOT** staged to
`services/dist/Chudware/libchudware.so` — the live MCP servers mmap it (overwriting mid-run =
SIGBUS). Stage + restart when the live sessions finish. Same maintenance rule as §B applies
(bump `chudware-geomcache/1` on any geometry-compiler output change).

## 2. Ranked menu — remaining opportunities (measured, not folklore)

### [P2, downgraded] Negative-volume guard integrates full inertia to check a sign
`core/doc/document.cpp:1557-1561` — `signed_vol()` calls
`BRepGProp::VolumeProperties(shape, gp)` (full Gauss **volume + centre-of-mass + inertia
tensor**) but only reads `gp.Mass()`. In the `all`/`boolean` workloads it's the top *compute*
self-time (`BRepGProp_Gauss::computeVInertiaOfElementaryPart` 4.3% + `BRepGProp_Face::Normal`
3.3%). **Downgraded after inspection:** the guard is **already incremental** — it runs only
inside the feature-execution loop (document.cpp:7013, `for k = solids_before..`), so
prefix-cached/reused features skip it entirely. The 5–8% is a *cold-build* figure (profiler
rebuilds every feature); real interactive edits already elide it via the prefix cache.
- The only remaining lever is the per-execution integration itself. The `Eps` overload
  (`VolumeProperties(shape, gp, eps)`) is **not a safe win**: it switches to adaptive
  Gauss-Kronrod, which can be *slower* than the fixed-order default on the simple planar /
  cylindrical faces these parts are made of, and it touches a B-rep integrity guard. Leave it
  unless a measured, quiet-box A/B shows a clear win.

### [P1] TBB parallel-boolean oversubscription under load / for tiny ops
`all` + `boolean` showed ~36% and ~35% self-time in `syscall` / `__lll_lock_wait` /
`__sched_yield` / `lock_wake`. Part is the load-16 artifact, but the structural cause is real:
`SetRunParallel(true)` (core/geom/booleans.cpp) spins up a TBB worker pool even for a
16-hole bolt-circle (16 tiny cuts). When N agent processes each do this on an already-saturated
box, the TBB pools thrash. **Fix candidate:** gate `SetRunParallel` on problem size (arg/tool
count or total face count above a threshold) so small ops stay single-threaded; optionally cap
TBB threads per process via `OSD_ThreadPool` when many engines share a box. **Must be
re-measured on a quiet machine** to separate the real win from the oversubscription artifact.

### [P2] tcmalloc into the desktop host binary (compile-only, ~5–10%)
RESULTS.md (2026-07-09) shipped tcmalloc for the **MCP** engine (server.py LD_PRELOAD reexec)
but the **host GUI** binary still uses glibc malloc. In-process runs (no preload) show
`_int_malloc`/`_int_free`/`calloc` ~3–4% self-time; OCCT boolean/mesh work is allocation-heavy.
Link `libtcmalloc_minimal` directly into the `host/` binary (host is the main executable, not a
dlopen, so linking works — unlike libchudware). Clean win; can't runtime-verify while the GUI is
live, so implement + document as ready-to-measure.

### [P2] Zero-copy AICM open (shave the remaining 75% memmove)
After §1, doc_open is a raw memmove of the mesh out of the zip. `mz_zip_reader_extract_to_heap`
allocates + copies; a STORED entry could instead be read via
`mz_zip_reader_extract_iter`/offset and the AICM parsed in place (or mmap the file). Bounded
upside (open is already ~5× faster) — do only if open latency still shows up.

### [DONE] Size-relative tessellation deflection (T-PERF7b) — already landed
Correction to the 2026-07-10 audit's "DECISION NEEDED": this is **already implemented and
active** (`core/doc/document.cpp:7208-7219`). `::geom::auto_linear_deflection` computes
`clamp(bbox_diag * 6e-4, 0.02 mm, 1.0 mm)`, so a ~50 mm part keeps the historical 0.05 mm
quality while multi-metre parts stop meshing at millions of triangles. No flag, no pending
decision — the biggest asymptotic gap is closed.

### [L, invasive] Import-heavy doc_open — embedded_brep is still inline in document.json
The §1 fix accelerates `cache.aicm`, but for **import-heavy** docs the dominant open cost is
`document.json` itself: `embedded_brep` is stored **inline as base64 inside the feature params**
(`session.cpp:8621/8666`), so a doc that imports a big STEP carries multi-MB base64 in
`document.json`, which is a KNOWN entry always written DEFLATE and read with inflate + CRC. This
is what T-PERF6b ("out-of-line content-addressed blob store") was meant to fix — it did **not**
fully land (blobs are still inline). Moving them to a separate STORED entry would make
`document.json` small again (fast parse) and let the blob use the §1 raw-read fast path. It's
deferred because it's a **schema/migration change** touching save/load, the PLM vault, and the
Python readers — not safe without that test coverage + a quiet box. Do it as its own task.

## 3. Verified-clean / already-done (don't re-audit)
Parallel booleans, tcmalloc-for-MCP, persisted geometry cache (§A/§B), incremental prefix
cache, body-tess cache, contention meters, per-feature timing, GUI async applyOps / drag /
mesh-load-off-GL / daemon timeouts / rebuilding chip, per-doc locking, checkpoint copy
amplification, owner-map checkpointing, sketcher interactive mode + component decomposition,
autosave token, live-search cancel, sampler cadence/symbolization. See T-PERF1..10 + the
2026-07-10 audit.

## 4. Execution order
1. ✅ doc_open zip-decode (§1) — **shipped to build-linux, verified (ctest green: aicm,
   geom_aicm, import_roundtrip, document, sketch3d_doc), awaiting dist deploy.**
2. Re-measure TBB parallel-boolean on a **quiet** box first (the ~35% syscall/lock/yield is
   partly the load-16 artifact), then cap TBB threads per process when many engines share a box
   — do NOT naively disable parallelism (it's a measured single-op win on a quiet box).
3. tcmalloc into host binary (§2 P2) — compile-only; needs a host rebuild, but
   `host/build-linux` is the **live GUI** (do it when the GUI is down).
4. Out-of-line embedded_brep blob store (T-PERF6b, §2 [L]) — the import-heavy-doc lever;
   schema change, its own task.
5. Volume-guard / zero-copy-open (§2) — marginal; only if a quiet-box profile still flags them.
