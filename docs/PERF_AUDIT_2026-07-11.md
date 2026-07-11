# Performance re-audit — post-T-PERF hotspots + doc_open zip-decode fix (2026-07-11)

Follow-up to `docs/PERF_AUDIT_2026-07-10.md` and the T-PERF1..10 wave (all landed). Every
number here is **fresh**, measured with the (now-working) in-process sampling profiler on
2026-07-11. It supersedes the pre-fix hotspot ranking in the earlier audit.

> **Measurement discipline.** The box was under real load (~16 on 12 cores — other agents
> modeling live) so *absolute* ms are inflated, but self-time **%** ranking within one op is
> stable (CPP_PERF.md). All runs were **in-process** (`AI_CAD_ENGINE_DAEMON=0`), a separate
> process that COW-maps the dist `.so` and contacts **no daemon** — it never touched the live
> hostd/MCP engines. Profiler cadence + symbolization now work (T-PERF10 fix): e.g. the `open`
> run got 3868 samples / 14 unresolved, vs the audit's "76 of 10k / all unresolved".
>
> Reproduce (safe, in-process):
> ```
> AI_CAD_ENGINE_DAEMON=0 python3 tools/profile_modeling.py --workload open --seconds 4 --hz 999
> AI_CAD_ENGINE_DAEMON=0 python3 tools/profile_modeling.py --workload all  --seconds 5 --hz 999
> ```

## 0. The profiler works now — use it, not the old audit's numbers

Confirmed working on Linux (the audit's "blind on Linux" predates the T-PERF10 sampler
rewrite: absolute-time `clock_nanosleep` cadence + ucontext leaf frame + ELF `.symtab` walk).
Two constraints when profiling on this machine:
1. **Always `AI_CAD_ENGINE_DAEMON=0`.** `core_bridge` defaults to daemon mode; without the
   opt-out the profiler routes into the **live shared engine** the other agents are using.
2. Point at a freshly-built engine with `AI_CAD_LIBAICAD=/abs/path/libaicad.so` to profile a
   change without staging over the live dist `.so`.

## 1. SHIPPED — doc_open was 81% zip CRC32 + inflate → now memmove-bound (~5.2×)

**The finding.** `open` workload (build a 3-feature part, save, reopen from the persisted
geometry cache — this is the **agent→GUI handoff path**, hit every time an agent publishes a
part):

| frame | before | after |
|---|---:|---:|
| `aicad!mz_crc32` | **47.3%** | 0.5% |
| `aicad!tinfl_decompress` | **33.8%** | 1.0% |
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

**Deploy note:** built + verified in `core/build-linux/bin/libaicad.so`. **NOT** staged to
`services/dist/AI-CAD/libaicad.so` — the live MCP servers mmap it (overwriting mid-run =
SIGBUS). Stage + restart when the live sessions finish. Same maintenance rule as §B applies
(bump `aicad-geomcache/1` on any geometry-compiler output change).

## 2. Ranked menu — remaining opportunities (measured, not folklore)

### [P1] Negative-volume guard computes full inertia to check a sign (~5–8% of every build)
`core/doc/document.cpp:1557-1561` — `signed_vol()` calls
`BRepGProp::VolumeProperties(shape, gp)` (full Gauss **volume + centre-of-mass + inertia
tensor**) but only reads `gp.Mass()`. This runs on **every committed solid on every compile**;
in the `all`/`boolean` workloads it's the top *compute* self-time
(`BRepGProp_Gauss::computeVInertiaOfElementaryPart` 4.3% + `BRepGProp_Face::Normal` 3.3%),
independent of the load artifact.
- **Fix candidate:** we only need the *sign*, not µm-accurate mass. The `Eps` overload
  `BRepGProp::VolumeProperties(shape, gp, eps)` terminates the adaptive Gauss integration
  early; a loose `eps` (e.g. 1e-2..1e-3 relative) is orders of magnitude more than enough to
  tell inverted from healthy. **Correctness-sensitive** (it's a B-rep integrity guard) — gate
  behind its own test pass: run the full core ctest + a near-zero-volume-body case before
  trusting it. Effort S, but do it *with* verification, not blind.

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
dlopen, so linking works — unlike libaicad). Clean win; can't runtime-verify while the GUI is
live, so implement + document as ready-to-measure.

### [P2] Zero-copy AICM open (shave the remaining 75% memmove)
After §1, doc_open is a raw memmove of the mesh out of the zip. `mz_zip_reader_extract_to_heap`
allocates + copies; a STORED entry could instead be read via
`mz_zip_reader_extract_iter`/offset and the AICM parsed in place (or mmap the file). Bounded
upside (open is already ~5× faster) — do only if open latency still shows up.

### [DECISION] Size-relative tessellation deflection (T-PERF7b — needs Hunter)
Still open from the 2026-07-10 audit. `document.cpp` tessellates at a **fixed 0.05 mm absolute**
deflection; a 2 m part meshes at millions of triangles (and then §1's memmove copies all of
them). Fix = bbox-diagonal-relative deflection → O(100k) tris on huge parts. Blocked because it
churns golden-image / mesh-hash baselines. **Recommended:** implement behind a **default-OFF**
env flag (`AICAD_TESS_RELATIVE=1`) so it ships without baseline churn and Hunter can A/B it,
then flip the default once the goldens are re-based. Not implemented here — it's Hunter's call.

## 3. Verified-clean / already-done (don't re-audit)
Parallel booleans, tcmalloc-for-MCP, persisted geometry cache (§A/§B), incremental prefix
cache, body-tess cache, contention meters, per-feature timing, GUI async applyOps / drag /
mesh-load-off-GL / daemon timeouts / rebuilding chip, per-doc locking, checkpoint copy
amplification, owner-map checkpointing, sketcher interactive mode + component decomposition,
autosave token, live-search cancel, sampler cadence/symbolization. See T-PERF1..10 + the
2026-07-10 audit.

## 4. Execution order
1. ✅ doc_open zip-decode (§1) — **shipped to build-linux, verified, awaiting dist deploy.**
2. Negative-volume guard `Eps` (§2 P1) — S, with a dedicated correctness test pass.
3. Re-measure TBB parallel-boolean on a **quiet** box, then gate on size (§2 P1).
4. tcmalloc into host binary (§2 P2) — compile-only.
5. Hunter decision on size-relative tess (§2 DECISION).
