# Profiling hotspots — 2026-07-15 (Lane P)

## Workload

**Primary:** scripted modeling workloads matching mega-21/22 rocket-plane mission geometry patterns (dense bolt rings + fillets on plates/airframe sections), driven in-process so live mega-22 hostd was never sampled or drained.

| Workload | Ops | Notes |
|---|---|---|
| `all` | box 100×80×40 + fillet(r=3, all edges) + bolt_circle(16 @ PCD70, Ø6) | `tools/profile_modeling.py --workload all` |
| `pattern` | box 140×140×20 + bolt_circle(48 @ PCD110, Ø5) | dense boolean/tess path |
| `rocket_airframe` | box 600×200×12 + bolt 36@160 + bolt 24@90 + fillet(1.5) | representative airframe plate |

**Capture method:** sampling profiler (`prof_start` / `prof_stop` / `prof_tree`) at 1000 Hz, modeling mode, `AI_CAD_ENGINE_DAEMON=0`, `AI_CAD_LIBAICAD=core/build-occt-linux/bin/libaicad.so`. Hostd (mega-22) left untouched during sampling; rebuild negotiated with UnusualBaboon after a2 finished.

**Artifacts:**
- `baseline_{all,pattern,rocket}.{folded,speedscope.json,svg}`
- `after_{all,pattern,rocket}.{folded,speedscope.json}` (+ trees/json)
- `baseline_*_tree.txt`, `after_*_tree.txt`
- `before_after_summary.json`, `after_wall.json`

## Top-10 frames (workload `all`, BEFORE)

### Exclusive (self-time)

| % | samples | frame |
|---:|---:|---|
| 19.8 | 2342 | `c!syscall` |
| 4.9 | 584 | `c!__sched_yield` |
| 4.3 | 509 | `c!__lll_lock_wait` |
| 4.1 | 485 | `TKTopAlgo!BRepGProp_Gauss::computeVInertiaOfElementaryPart` |
| 2.9 | 345 | `TKTopAlgo!BRepGProp_Face::Normal` |
| 2.8 | 329 | `c!__GI___lll_lock_wake` |
| 2.5 | 295 | `TKG3d!GeomAdaptor_Surface::D1` |
| 1.9 | 228 | `c!__syscall_cancel_arch_start` |
| 1.9 | 223 | `m!0x83e64` |
| 1.5 | 177 | `TKMath!ElSLib::CylinderD1` |

### Inclusive (total, non-interpreter)

| % | samples | frame |
|---:|---:|---|
| 100 | 11843 | `aicad!aicad_call_inner` → `doc_apply_ops` → `compile_document` |
| 40.1 | 4745 | `aicad!geom::cut` / `run_pair<BRepAlgoAPI_Cut>` |
| 36.9 | — | `BRepGProp_Gauss` family (volume + area integrations) |
| 25.9 | 3065 | `aicad!geom::tessellate_body` |
| 17.3 | — | `BRepGProp::VolumeProperties` call sites |
| 7.1 | — | `BRepGProp::SurfaceProperties` call sites |
| 6.1 | 724 | `aicad!geom::fillet_edges` / `blend_drop_failing` |
| 5.0 | 588 | **`aicad!geom::shape_area`** ← our-code hotspot |
| 2.3 | 276 | `aicad!doc::resolve_geom_query` |
| 1.8 | 214 | `aicad!geom::fuse_many` |

### Pattern workload inclusive highlights (BEFORE)

| % | frame |
|---:|---|
| 50.8 | `geom::tessellate_body` |
| 32.8 | `geom::cut` |
| 17.6 | `BRepGProp_Gauss` |
| 3.5 | **`geom::shape_area`** |

## Highest-leverage OUR-code hotspot

**`geom::shape_area` + post-tessellate body volume stats** (`core/geom/surfacing.cpp`, `core/doc/document.cpp`).

Why this and not OCCT-internal cut/tess:
- Pure OCCT frames (`BRepAlgoAPI_*`, `BRepMesh_*`) are not ours to rewrite.
- `shape_area` is our wrapper around `BRepGProp::SurfaceProperties` and sits at **~5% inclusive** on `all` / **~3.5%** on `pattern`.
- Body-stats volume uses exact `VolumeProperties` *after* `tessellate_body` already attached a triangulation — paying for exact Gauss again is pure waste.
- Microbench (24-hole plate, meshed):  
  - volume exact 1.532 ms → UseTri **0.548 ms (~2.8×)**  
  - area exact 1.977 ms → UseTri **0.527 ms (~3.7×)**  
  - relative mass error ≪ 0.1% (display/stats only; mass eval still exact).

Secondary our-code change (same rebuild): size-gate `SetRunParallel` in `core/geom/booleans.cpp` so tiny ops stay serial (addresses PERF_AUDIT 2026-07-11 P1 oversubscription under multi-agent load).

## Optimization landed

### 1. Triangulation-backed GProp for body stats (primary)

| File | Change |
|---|---|
| `core/geom/surfacing.cpp` `shape_area` (~L93–104) | `SurfaceProperties(..., UseTriangulation=true)` |
| `core/geom/surfacing.cpp` `face_centroid` (~L121–128) | same |
| `core/doc/document.cpp` post-tess body volume (~L7238–7249) | `VolumeProperties(..., UseTriangulation=true)` after tessellate |

Behavior: when a face triangulation is present (always true on the post-tessellate stats path), OCCT integrates over the mesh; without triangulation it falls back to exact geometry (unmeshed callers unchanged). Mass/CoM eval paths still use exact VolumeProperties.

### 2. Size-gated boolean parallelism (secondary)

| File | Change |
|---|---|
| `core/geom/booleans.cpp` `run_pair` / `fuse_many` / `cut_many` / `fuse_unify` | `SetRunParallel` only when face_count≥16 or tool_count≥4 |

## Before / after (same workloads)

### Sampling inclusive % (our hotspot family)

| metric | all before | all after | pattern before | pattern after |
|---|---:|---:|---:|---:|
| `geom::shape_area` inclusive | **4.96%** | **1.56%** | **3.49%** | **1.43%** |
| `BRepGProp_Gauss` inclusive | 36.94% | 23.40% | 17.60% | 9.02% |
| `SurfaceProperties` inclusive | 7.06% | 3.77% | 3.51% | 1.53% |
| MeshProps self (new path) | 0% | 1.01% | 0% | 0.94% |

MeshProps appearing after is the smoking gun that UseTriangulation is active.

### Wall / throughput (12 s sample windows, same harness)

| workload | before builds/12.1s | after builds/12.1s | ms/build before | ms/build after | Δ |
|---|---:|---:|---:|---:|---:|
| `all` | 140 (11.6/s) | **154 (12.7/s)** | 86.4 | **78.6** | **~9.1% faster** |
| `pattern` | 83 (6.9/s) | **87 (7.2/s)** | 145.8 | **139.1** | **~4.6% faster** |

### Microbench (isolated GProp, meshed 24-hole plate)

| call | before ms | after ms | speedup |
|---|---:|---:|---:|
| VolumeProperties | 1.532 | 0.548 | **2.80×** |
| SurfaceProperties | 1.977 | 0.527 | **3.75×** |

## Parity / tests

- ctest (occt-linux): `document`, `ops`, `geom_aicm`, `hole`, `import_roundtrip`, `profile_extrude`, `profile_arc`, `prof`, `prof_phases` — **PASS**
- `prof_tree` binary not built in this tree (Not Run — pre-existing, not a regression)
- Volume stability on `all` workload: 5/5 identical volumes (spread 0)
- Full `scripts/gate.sh engine` timed out in this session under load; focused geometry suites above cover the changed contracts
- engine_oracle golden fixture `docs/contracts/fixtures/box.aicm` missing (pre-existing FileNotFoundError, unrelated)

## Rebuild

- `flock /tmp/billet-build.lock ./build.sh daemon` after UnusualBaboon cleared a2
- Touched `core/build-occt-linux/bin/libaicad.so` + `aicad-hostd` (a3 later reattached)

## Non-goals / deferred

- `blend_drop_failing` on pathological whole-body chamfer (72% on a deliberate fail-all chamfer workload) — already bisect-optimized (T-PERF10); remaining cost is OCCT ChFi3d
- Exact volume guard in the feature loop (`signed_vol`) left exact — integrity check, not display stats
- Zero-copy AICM open / tcmalloc-in-host — tracked in PERF_AUDIT, out of scope
