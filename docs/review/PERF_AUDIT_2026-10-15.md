# Performance audit — measured exact optimizations and remaining bottlenecks (2026-10-15)

This audit follows the [2026-07-11 report](PERF_AUDIT_2026-07-11.md), which remains historical evidence, not a current performance baseline. This pass combines sequential before/after measurements, implemented hot-path changes, numerical/output comparisons, and a broader **static-only** inventory. It is not a claim that every product subsystem was benchmarked.

> **Measurement discipline.** Work ran in isolated private in-process engines, a socketpair transport fixture, standalone simulation/ECAD drivers, and hidden real OpenGL contexts. No live daemon was stopped, no user session was interrupted, and no build was staged into `services/dist`. Timings below are observed on this workstation, not portable product guarantees. Correctness comparisons take precedence over throughput. Geometry quality, cache trust, and solver thresholds were not reduced.

## 0. Machine, binaries, scope and evidence

| Item | Recorded configuration |
|---|---|
| CPU | AMD Ryzen 5 7600X3D; 12 logical CPUs |
| GPU | NVIDIA GeForce RTX 3080 Ti, 12 GiB |
| OS | Fedora 44, Linux 6.19.10-300.fc44.x86_64 |
| Native build | Release, GCC 16 |
| CUDA / driver | CUDA 13.3 / NVIDIA 580.159.04 |
| Modeling/IPC interpreter | CPython 3.11.15, provisioned `.venv-mcp` |
| Modeling thread environment | `CHUDWARE_SIM_THREADS=4`, `OMP_NUM_THREADS=4`; complete affinity/environment retained in JSON |
| CUDA configuration | Optional default **OFF** preserved; local `core/build-occt-linux` enabled it, `core/build-linux` retains CPU fallback |

The engine identities are **SHA-256 of the library bytes**, not source commits. Both builds report the same dirty-tree build ID, so that ID alone does not distinguish them:

```text
before c0fafef7fddc0dc45e2f8cb266cbf96b3a0cd2cab0920e7608ec32ccdf685471
after  2159842c0910d47e0633cf67f63a67ed9d1abc6f073eedb010533c1e8a33d433
```

**Durable reproduction inputs:** the source-linked tools in §7, [schema-2 baseline](../../tools/perf_baseline.json), production changes linked in §2–5, and this report. **Ephemeral raw evidence:** [scratch/performance-2026-10-15](../../scratch/performance-2026-10-15/). Scratch files, copied old binaries/source, flame graphs, screenshots and profiler databases are local run artifacts, not a durable release bundle; reproduction must not depend on their continued existence. Do not copy the whole Nsight databases into tracked reports: they include environment records. The initially captured `environment.json` used Python 3.14.6 for discovery; the actual modeling and IPC records identify their measurement interpreter as 3.11.15.

Primary local evidence:

- Modeling: [modeling-baseline.json](../../scratch/performance-2026-10-15/modeling-baseline.json), [modeling-after.json](../../scratch/performance-2026-10-15/modeling-after.json), `baseline-open.report.json`, `baseline-pattern-idle.report.json`, and their folded/Speedscope/SVG outputs.
- Selectors: [picks-comparison.json](../../scratch/performance-2026-10-15/picks-comparison.json), `picks-before.json`, `picks-after.json`.
- Transport: [ipc-original.json](../../scratch/performance-2026-10-15/ipc-original.json), [ipc-changed.json](../../scratch/performance-2026-10-15/ipc-changed.json).
- ECAD: `ecad-before.json`, `ecad-before-{mixed-12-0.25,boundary-12-0.1}.metrics.json`, and `ecad-final-{dense,mixed,boundary}.{json,metrics.json}`. Dense before timings below were observed by the integrating run; the dense canonical output remains available.
- Simulation: [lbm-before-128.jsonl](../../scratch/performance-2026-10-15/lbm-before-128.jsonl), [lbm-after-128.jsonl](../../scratch/performance-2026-10-15/lbm-after-128.jsonl), `cuda-dispatch-before.log`, `lbm-{before,after}-nsys.nsys-rep` and corresponding `.sqlite` exports.
- GL: [viewport-before.json](../../scratch/performance-2026-10-15/viewport-before.json), [viewport-after.json](../../scratch/performance-2026-10-15/viewport-after.json), full `viewport-{before,after}.csv`, ID attachment PNGs, and GUI `pick`/`m1` logs and captures.
- Other: [hash-before.json](../../scratch/performance-2026-10-15/hash-before.json), [hash-after.json](../../scratch/performance-2026-10-15/hash-after.json) (**key=value text despite the extension**), [fea-before.log](../../scratch/performance-2026-10-15/fea-before.log), [dfam-before.log](../../scratch/performance-2026-10-15/dfam-before.log), and cross-version cache logs/archives.

## 1. Measured summary

Modeling has one validated untimed warmup and nine timed samples per workload. BUILD includes `doc_new + doc_apply_ops`, geometry/tessellation and Python/ctypes reply decoding; validation and `doc_close` are outside the timed interval. OPEN_WARM times `doc_open` of a freshly saved persisted-cache fixture, excluding fixture construction and close. It is **not** cold disk I/O or a feature rebuild. p95 uses nearest rank; with nine samples it is the maximum.

| Workload | Before median / p95, ms | After median / p95, ms | Interpretation |
|---|---:|---:|---|
| Box | 0.891 / 1.040 | 0.734 / 2.315 | Tiny interval; after tail is worse, not a robust speedup claim |
| Fillet | 48.038 / 57.796 | 36.091 / 39.342 | Lower observed wall time; no fillet algorithm change isolated |
| Chamfer | 13.679 / 14.369 | 13.260 / 13.731 | Small difference |
| Shell | 4.789 / 5.161 | 4.472 / 4.870 | Small difference |
| Boolean | 108.182 / 123.029 | 98.401 / 101.667 | Lower observed wall time; CPU median is essentially unchanged |
| Pattern | 221.695 / 248.065 | 219.863 / 229.790 | Dominant geometry/tessellation work remains |
| Heavy mix | 130.575 / 134.863 | 129.047 / 132.106 | Essentially unchanged |
| Verified warm open | **65.470 / 68.094** | **36.083 / 37.265** | **1.81× throughput-equivalent; 44.9% less latency** |

Warm-open process CPU median fell from 65.007 to 35.286 ms. Other wall-time movements are reported honestly, not all attributed to the edits: e.g. fillet CPU medians are 88.121 → 85.596 ms and boolean 388.612 → 391.144 ms. The after record passes the matched baseline comparison with no regressions under the configured 25% **and** 1 ms rule; that is not a full repository-gate result.

The original raw reports recorded 1,108,672,512 bytes before and 440,602,624 after using Linux `getrusage(RUSAGE_SELF).ru_maxrss`. **These values are launcher-contaminated and are not a usable application-memory comparison.** An independent tiny child demonstrated that Linux retains the launcher's pre-exec high-water mark: `getrusage` reported 430,276 KiB while that executable image's `/proc/self/status` `VmHWM` was 10,272 KiB. The corrected helper reads Linux `VmHWM`; an actual corrected-helper observation was 20,242,432 bytes versus inherited 440,602,624 bytes, and a separate two-sample warm-open CLI run passed with 235,053,056-byte `VmHWM`. These observations use different scopes/runs and establish a measurement correction, **not a memory or speed improvement**. Even corrected process-wide image peak RSS is diagnostic, not a per-operation allocation delta. Raw original reports remain unchanged; evidence is in `rss-launcher-diagnostic.log`, `rss-launcher-corrected.json` and `rss-corrected-benchmark.json` under the scratch evidence directory.

## 2. Implemented — verified cache open and direct face descriptors

### SHA-256 and cache publication

[core/util/sha256.h](../../core/util/sha256.h) now consumes complete input blocks directly and uses runtime-guarded x86 SHA-NI where supported, retaining the scalar fallback. Dispatch is guarded by CPU capability; it is not a blanket architecture requirement. Hash semantics and cache verification are unchanged. [core/doc/session.cpp](../../core/doc/session.cpp) publishes cached AICM bytes directly through the existing pointer/size handoff rather than first allocating and copying a temporary mesh vector. The handoff's necessary publication copy remains; this is not zero-copy file open, nor a claim that every checkpoint copy was removed.

The retained [hash driver](../../core/tools/hash_profile.cpp), hashing the same 64 MiB payload 32 times, measured scalar **5.514434 s / 371.389 MiB/s** versus SHA-NI **0.933434 s / 2,194.048 MiB/s**, a 5.91× hash-throughput ratio. Both returned:

```text
e966427143da9f7e64951e8f8a9f435a540fcef5247558ed452594c71ffdd458
```

That microbenchmark supports the hashing mechanism; the end-to-end open improvement is the separately measured 1.81× above, not 5.91×. The open sampling record contains 1,915 samples / 2,029 ticks, 1,915 ticks with a target, zero dropped samples and 19 unresolved frames. It is useful attribution, not all-thread CPU accounting.

Post-review independent checking also matched the hash driver's output to Python `hashlib` at **15 input sizes, including 64 MiB**. This is digest validation separate from the timed repeated-payload throughput run.

Cross-version **old save → new open** and **new save → old open** both reported `warm=true`, `cache_hit=true`, `cache_trust=verified`, and `features_executed=0`. Unedited resave preserved that file's `cache.aicm`, `cache.brep` and `cache.ckpt` bytes. This compares each archive with its own resave; it does not assert that independently saved old/new archives have identical checkpoint bytes. See `cache-open-saved-before.step+.log` and `cache-open-saved-after.step+.log`. Cache integrity, format compatibility, and feature-history restoration were not traded away.

### Face selector generation

[core/doc/selectors.cpp](../../core/doc/selectors.cpp) describes the selected face directly instead of describing every face and discarding all but one result. The descriptor logic is shared with full enumeration, preserving surface classification, area, location, ownership and selection semantics. No per-shape memo or approximate geometry was needed.

The fixed selector benchmark performed **five complete measured sweeps** of every face, edge and vertex of its fixture, not a time-limited hot subset. All **442** selector replies compared exactly, including topology/workload data, with no ignored selector fields; run metadata and timing are excluded. Face median was **7.884974 → 0.792577 ms (9.95×)**; face mean was 7.998676 → 0.529814 ms (15.10×). Edge and vertex latencies are effectively unchanged (medians 37.706 → 38.522 µs and 49.959 → 50.820 µs). The all-kind median barely moves because cheap edge/vertex calls dominate that statistic; do not substitute the all-kind mean for typical face latency.

## 3. Implemented — bounded PCB fill and linear Python IPC framing

### Copper-zone filling

[core/ecad/zonefill.cpp](../../core/ecad/zonefill.cpp) restricts carving and thermal-relief work to conservative cell ranges around the shape's influence, searching actual cell centres and retaining the original geometric predicate inside the range. Conservative padding and nonfinite/overflow fallback preserve boundary cells, negative origins and extreme coordinates. Contour extraction now advances a monotonic insertion-order frontier rather than repeatedly rescanning exhausted starts; it also builds/consumes one adjacency map instead of copying the map. Traversal order and LIFO edge selection remain unchanged. This removes redundant search/copy work, not copper detail or DRC checks. The separate session-level board/fill copy path is not claimed eliminated.

| Fixture | Before fill, ms | Final fill median, ms | Final DRC / ratsnest median, ms | Sampling caveat |
|---|---:|---:|---:|---|
| Dense: 2,500 pads, 49 tracks, one zone, 0.25 mm | **7,936.428** | **113.815** | 972.090 / 10.449 | Three samples before and after |
| Mixed: 144 pads, 11 tracks, two zones, 0.25 mm | 12.405 | 3.322 | 25.114 / 0.284 | One original sample; three final samples |
| Boundary: 144 pads, 11 tracks, two zones, 0.1 mm | 69.368 | 16.364 | 128.382 / 0.270 | One original sample; three final samples |

Dense before fill samples were **[7834.366216, 7936.427571, 8265.276462] ms**. Its fill median improves 69.73×, while DRC (before 978.122388 ms) and ratsnest (before 10.623532 ms) stay approximately unchanged. It would be wrong to call the entire board compile 69.73× faster: after the fill change, DRC dominates this fixture.

Canonical dense output is **byte-identical** to `ecad-before.json`: 1,705,341 bytes including the trailing newline (the metrics' `output_bytes=1705340` excludes it), FNV-1a64 `86ad0ef49c9b89cb`. Mixed/boundary output fingerprints also match the originals: `f0f1865e860d0e99` and `21d80f2ff0d1649f`. Existing unrouted/zone-clearance/island findings are retained, not suppressed. These are intentionally non-clean routing fixtures, not proof that a board with violations is fabrication-ready.

### Daemon-client receive framing

[services/mcp/daemon_bridge.py](../../services/mcp/daemon_bridge.py) uses a mutable receive buffer and a forward scan position instead of immutable-byte concatenation plus repeated scans from byte zero. Framing work is linear in received bytes; JSON decoding still costs what the payload requires. Events, coalesced replies, stray lines, partial lines and EOF semantics remain part of the transport contract; no lost mutation reply is replayed.

The retained socketpair profiler calls `DaemonClient._call_raw` with an event preceding each reply. Seven repetitions include request serialization, transport, event/reply framing and decoding; import, reply construction and full output/order validation are outside timing. This is **not** a real daemon's geometry time or a network/gateway benchmark.

| Exact JSON reply size | Before median / p95, ms | After median / p95, ms |
|---|---:|---:|
| 512 KiB | 4.645 / 5.013 | 4.929 / 6.901 |
| 4 MiB | 41.038 / 46.616 | 36.429 / 43.116 |
| 16 MiB | **229.623 / 240.787** | **165.677 / 179.464** |

The largest fixture is 27.8% lower latency (1.39× ratio). The smallest is slower in this run; no universal small-message speedup is claimed. Original and changed module hashes are recorded in the JSON independently of engine hashes.

## 4. Implemented — exact viewport pick readback reuse

[host/src/render/id_pass.cpp](../../host/src/render/id_pass.cpp) and [id_pass.h](../../host/src/render/id_pass.h) cache a pick for an unchanged rendered ID frame and identical query, invalidate on document/frame/query changes or failed rendering, obtain the centre and neighbourhood from **one block read**, and cache shader uniform locations after link. Centre-hit and vertex/edge/face precedence remain unchanged; the work changes readback frequency rather than visual quality or supersampling.

The retained hidden-context GL driver used the actual **NVIDIA GeForce RTX 3080 Ti/PCIe/SSE2** renderer, 320×240 final size, supersample 3, and both R32UI and RGBA8 attachments. Full timing-free before/after hit CSVs compared identical. Both modes have fingerprint **`6a8f5a5d2a9d6a04`**, with **zero GL errors**. Stages cover camera, visibility hide/restore, clip normal/offset/count, topology, final/frame dimensions, faces-only/full topology, failed-render recovery and document recovery. Changed coordinates/radius/method and invalid queries are covered by explicit memo guards.

Held hover over a face for **10,000 repeated queries**:

| Readbacks | Before | After |
|---|---:|---:|
| First prefer-face query | 2 | 1 |
| Following 10,000 identical queries | **20,000** | **0** |

The nanosecond-scale after cache-hit timings measure a CPU memo lookup, **not application FPS**, frame pacing or GPU render throughput. Moving queries still require readbacks. No FPS improvement is asserted. A separate Xvfb smoke passed but also selected NVIDIA; it must not be described as llvmpipe/software rendering merely because Xvfb was used.

The real desktop `pick` scenario selected **face 6**. The `m1` scenario completed box → fillet → shell → fillet edit → save → reopen, restored the cached mesh and retained the **three-feature editable history**. The integrating run visually inspected the screenshots; their existence alone is not the acceptance evidence. See `gui-after-pick.{log,png}`, `gui-after-m1.log` and the M1 stage captures under `scratch/m1_shots`, especially [m1_06_reopen.png](../../scratch/m1_shots/m1_06_reopen.png). M1 does not write `gui-after-m1.png`.

## 5. Implemented — usable, exact CUDA LBM with bounded transfers

### Correctness repair before performance claims

The original CUDA-enabled dispatcher did not compile (`cuda-dispatch-before.log`), and the original CUDA force estimator read the wrong population slots/time in the bounce-back sequence and did not match CPU force-window/convergence semantics. The baseline standalone driver deliberately called the original CUDA payload directly, bypassing the broken dispatcher; its successful process exit with `baseline_mismatch_allowed` is **not a correct numerical result**.

For the asymmetric 128×64×64, 200-step fixture, original CPU drag was 0.0722894302368164 N versus old CUDA 1.224221664428711 N; old `parity_pass=false`, including force and convergence disagreement. Old GPU timing is retained only as defective-path cost evidence. It is not a reference solution or an accuracy/performance trade-off to preserve.

[core/sim/lbm.cpp](../../core/sim/lbm.cpp) and [lbm_cuda.cu](../../core/sim/lbm_cuda.cu) now preserve the CPU numerical contract through:

- Compact boundary-slot lists in CPU traversal/direction order; gather only required populations before bounce-back, with the CPU's float32 pairwise reduction order retained on the host.
- Device initialization rather than constructing/uploading a full host lattice; no full-distribution download at every force sample or final force evaluation.
- CPU-equivalent sampling cadence, force-window averaging, continuity/mass-balance calculation, residual/convergence decision and early stopping. Cooperative cancellation and memory-pressure stops remain terminal rather than silently restarting a CPU solve.
- Separately rounded float32 arithmetic, including `--fmad=false`, rather than loosening numeric tolerances to hide drift.

[core/CMakeLists.txt](../../core/CMakeLists.txt) keeps CUDA optional/default OFF. `CUDA_RESOLVE_DEVICE_SYMBOLS ON` resolves the simulation archive's device symbols with its own **sm_75 + sm_86** architecture list, preventing downstream C++ device-link defaults from dropping the Ampere image. The local OCCT build enables CUDA; the ordinary CPU build remains supported. CUDA compilation on this new GCC uses `-allow-unsupported-compiler`; that compatibility override is not necessary on every compiler.

### Uninstrumented results

128×64×64 = **524,288 cells**, asymmetric solid mask, **24,054 boundary links**, 200 steps, three runs per backend with **first CUDA context initialization included**:

| Backend | Run times, seconds | Median, seconds |
|---|---|---:|
| CPU after | 2.033377, 1.981337, 2.032842 | **2.032842** |
| CUDA after | 0.342150, 0.272302, 0.273258 | **0.273258** |
| Original defective CUDA payload | 1.148244, 1.081741, 1.075104 | 1.081741; invalid numerical baseline |

New CPU and GPU fields, forces and convergence fingerprints are **bit-identical** for all three runs: **`9864317740161578793`**. The parity records show zero maximum field difference, zero force differences, zero residual/mass-imbalance differences, and matching 200-step horizons. Both correctly report **not converged** at that horizon; equality is not a claim of physical convergence. Determinism and parity summaries pass.

The new GPU is **7.44× faster than the measured CPU median on this fixture**. This does not establish that every grid size, short run, model or GPU is faster; startup and transfer overhead matter, and no broad crossover-size sweep is claimed. The original incorrect GPU median is not used as the product speedup denominator.

### Nsight Systems transfer attribution

Actual **Nsight Systems 2026.1.3** CUDA traces ran one CUDA-only solve before and after at the same grid/horizon. The SQL `CUPTI_ACTIVITY_KIND_MEMCPY` records show:

| Transfer direction | Original copies / bytes | Changed copies / bytes |
|---|---:|---:|
| Host → device | 3 / 40,370,252 | 3 / 716,796 |
| Device → host | **55 / 2,040,528,896** | **25 / 10,312,932** |

D2H volume is **197.86× smaller**. Instrumented device-copy times were 118.446 → 0.457 ms; these are diagnostic trace timings, not substitutes for uninstrumented solve wall time. A one-run CUDA-only trace is not numerical parity proof; the separate CPU/GPU comparisons above provide that evidence. Raw captures/SQLite exports remain under the scratch `lbm-before-nsys` and `lbm-after-nsys` prefixes.

Product GPU investment should target this regular, lattice-based LBM work. Exact OCCT topology, small dense sketch solves and the currently measured FEA mesher are not justified GPU-port targets by this evidence.

## 6. Audit inventory and remaining ranked bottlenecks

**Measured** means an executed workload or profile in this pass, not merely code inspection. **Static-only** means a candidate/complexity observation, not measured severity or implemented optimization.

| Area | Coverage / disposition |
|---|---|
| Modeling, persistence, selectors | Measured build/open/pick corpus; source inspection of compilation, checkpoints, exact operations, tessellation, ownership and assembly paths. Hash/publication/one-face changes implemented; geometry algorithms and tolerances unchanged. |
| Desktop viewport | Real GL pick driver plus actual `pick`/`m1` captures; static renderer, overlays, ID pass and event/mesh-routing review. Pick memo/block read/uniform cache implemented. No full interactive orbit/FPS benchmark. |
| LBM | CPU/GPU wall/phase/parity runs and actual Nsight transfer traces. CUDA repair/transfer work implemented. CPU-only pipe LBM, broader flow and topopt solver paths were static-only. |
| FEA | Executed beam mesher/solve profile; static review of static/modal/thermal/buckling/fatigue/contact/adaptive solve and topopt paths. No FEA optimization or GPU speedup claimed. |
| ECAD | Executed dense/mixed/boundary zone-fill/DRC/ratsnest drivers, canonical comparisons; static schematic/netlist/autoroute/session inspection. Zone-fill changes implemented; DRC pair search and autorouter not optimized. |
| Python IPC / hostd | Measured socketpair client framing only; static client/bridge, server/registry/event fan-out, worker bridge and native transport review. No end-to-end daemon speedup claimed. |
| DFAM | Executed 61,440-triangle analysis/cProfile; static checks/geometry/orientation inspection. No DFAM optimization implemented. |
| PLM / AI-VC | **Static-only** fingerprint/delta/chain/materialization and graph/cache scan paths, with partial vault/Git/server review. No runtime profile or speed claim. |
| CAM | **Static-only** heightfield/drop-tool, stock simulation, geometry/offset/raster, toolpath generation/emission/audit and session paths. No CAM benchmark or optimization claimed. |
| Sketch | **Static-only** sketch geometry/Jacobian/diagnostics and small dense solver characteristics; no sketch latency benchmark or solver change. |
| Piping / hydraulics | **Static-only** piping solve/Jacobian/fittings and hydraulic transient Newton/evaluation paths; no measured hydraulic/piping performance or optimization. |
| Web gateway | **Static-only** policy loading, frame validation/rewriting, auth/workspace and quota paths. No gateway timing, live account traffic or policy/auth caching change. |

The inventory is broad, not exhaustive: detailed peripheral reading did not cover every import/export/library module, every CAM operation family, every MCP tool, every gateway route or every platform branch. Static audit observations must not be promoted to benchmark results.

### Measured priorities left unoptimized

1. **FEA mesh generation, not a tiny linear solve.** The 80×20×20 mm beam at fineness 0.30 spends **2,186.64 ms meshing versus 301.11 ms solve**, total 2,499.26 ms. Mesh: **8,281 nodes, 5,184 tet10 elements, 24,843 DOF**. The actual solver was four-thread `CholmodSupernodalLLT`, not an inferred slow fallback; analyze/factor/solve alone was 242.10 ms. Optimize/profile mesher phases first, preserving delivered size and quality; a GPU solve cannot remove this measured mesh cost. Sources: [fea_profile.cpp](../../core/tools/fea_profile.cpp), [fea_run.cpp](../../core/sim/fea_run.cpp), raw `fea-before.log`.
2. **DFAM ray-wall analysis.** On **61,440 triangles**, full analysis is **1.515512 s**; `min_wall` accounts for **1.045 s cumulative**, with ray casting/intersections prominent. This is one cProfile workload, not a before/after optimization. Attribute ray traversal and repeated geometry work before changing it; preserve exact findings and output digest. Sources: [checks.py](../../services/dfam/checks.py), [geometry.py](../../services/dfam/geometry.py), raw `dfam-before.log`/`.prof`.
3. **PCB DRC after the fill fix.** Dense DRC is now about **972 ms**, versus 114 ms fill. Static pair-loop/AABB opportunities in [drc.cpp](../../core/ecad/drc.cpp) need their own profile, exact violation-order comparison and boundary cases. They were not implemented in this wave.
4. **OCCT tessellation on patterned geometry.** An idle pattern profile's measured native phase contains **155.524 ms mesh / 167.837 ms total tessellation**. This phase observation is distinct from the nine-sample end-to-end pattern median (~220 ms). Inspect mesh construction/cache reuse and allocation before altering parallelism. No coarsening, reduced supersampling or serial/parallel BRepMesh cutover was made; exact mesh identity would need proof. Source: [tessellate.cpp](../../core/geom/tessellate.cpp), raw `baseline-pattern-idle.report.json`.

### Static-only candidates, not ranked by invented timings

- Kernel/assembly: repeated volume integrations, checkpoint owner-map copies, full-result/decoder copies and per-body display meshing. Attribute first; retain non-destructive OCCT history, signed-volume guards and cache identity.
- Viewport: per-face/per-edge overlay GL objects and redundant renderer mesh copies. Native supersample-3 fill cost is a visual-quality decision, not a free optimization. Browser/WebGL timing was not exercised.
- PLM/AI-VC: backing-file fingerprint plus authored-container reread, bytewise delta construction, repeated chain materialization and graph tree walks/cache parse. Preserve canonical materialization and stat/integrity semantics before introducing caches.
- CAM: full-square drop-tool scans, stock swept-AABB work, repeated slice/offset passes, per-move string formatting and JSON snapshots. Exact toolpath/stock comparisons are missing, so no change was made.
- Sketch/piping/hydraulics: repeated finite-difference allocations and dense Jacobian/LU work; changing Jacobian reuse or pivoting may change numerical behaviour. No optimization justified without a representative latency/convergence profile.
- Hostd/gateway: native result/event copies under registry locks, repeated policy parsing and recursive Python frame work, plus auth/quota database/filesystem work. Tenancy/authority checks must not be cached away on performance inference alone. The socketpair result does not establish gateway cost.

## 7. Reproduction and regression contract

These commands are runnable examples from the repository root, **not additional checks claimed executed by this document**. They select a private library and create only scratch results. Use the provisioned interpreter and the same machine/affinity/thread environment for comparisons. Run workloads sequentially, not beside a build or competing solve. Never stage over a live mapped library or stop a user's daemon to obtain a quieter benchmark.

### Optional CUDA build; CPU fallback remains available

For an already provisioned local OCCT build tree:

```bash
cmake -S core -B core/build-occt-linux -DCMAKE_BUILD_TYPE=Release \
  -DCHUDWARE_WITH_CUDA=ON -DCMAKE_CUDA_FLAGS=-allow-unsupported-compiler
cmake --build core/build-occt-linux --target chudware lbm_profile ecad_profile hash_profile
cmake --build host/build-linux --target profile_viewport
```

The explicit `CMAKE_CUDA_FLAGS` override is for a host compiler newer than nvcc's supported ceiling (GCC 16 here); omit it on a supported compiler. The target also carries the compatibility flag in current CMake. Do not remove `--fmad=false` to chase a misleading parity-tolerance win. A CPU-only configuration uses `-DCHUDWARE_WITH_CUDA=OFF` in `core/build-linux`; CUDA is not required for ordinary modeling or these Python profiling tools.

### Private modeling baseline, samples and selectors

```bash
mkdir -p scratch/performance-local
export CHUDWARE_ENGINE_DAEMON=0
export CHUDWARE_LIBCHUDWARE="$PWD/core/build-occt-linux/bin/libchudware.so"
export CHUDWARE_SIM_THREADS=4 OMP_NUM_THREADS=4
PY="$PWD/.venv-mcp/bin/python"

# Capture on the baseline engine, then compare after pointing the env at the new one.
"$PY" tools/perf_regression.py --iters 9 \
  --baseline scratch/performance-local/baseline.json --update
"$PY" tools/perf_regression.py --iters 9 \
  --baseline scratch/performance-local/baseline.json \
  --json scratch/performance-local/after.json
"$PY" tools/profile_modeling.py --workload open --seconds 4 --hz 1000 \
  --out scratch/performance-local/open
"$PY" tools/profile_modeling.py --workload pattern --seconds 4 --hz 1000 \
  --out scratch/performance-local/pattern

"$PY" tools/profile_picks.py run --sweeps 5 --out scratch/performance-local/picks.json
# Repeat against each library with distinct output names, then:
"$PY" tools/profile_picks.py compare scratch/performance-local/picks-before.json \
  scratch/performance-local/picks-after.json --out scratch/performance-local/picks-comparison.json
```

Durable sources: [perf_regression.py](../../tools/perf_regression.py), [profile_modeling.py](../../tools/profile_modeling.py), [profile_picks.py](../../tools/profile_picks.py), [test_perf_regression.py](../../tools/test_perf_regression.py), [test_profile_modeling.py](../../tools/test_profile_modeling.py). All three engine-backed drivers use the shared scoped `_private_engine` context during execution: it forces private in-process discovery and restores the caller's settings on exit, including failure. Merely importing a profiling module does not change engine discovery. Sampling retains engine provenance, operation results, complete sampler output and phase data, and refuses failed/rolled-back features rather than celebrating their short runtime. The retained invalid-fillet report has `ok=false` and no sampler run; it is not a fast successful workload. The sampler observes its registered modeling thread, not all OpenMP/TBB workers; use process CPU clocks or `perf` for all-thread costs.

Independent review reproduced four harness defects after the initial smokes: importing regression code changed three discovery environment variables; deleting real saved-cache entries could still be reported as successful warm-open sampling; a directory at the folded-stack export path produced `ok=false` but valid throughput; and a real CPU governor stop at step 10 still emitted positive MLUPS. The corrected methods isolate discovery only during execution, require written cache plus `cache_hit=true`, `warm=true`, zero executed features and no hydrate error for sampled opens, finalize throughput validity after export validation, and emit `mlups:null` for unsuccessful solver runs. Regression coverage uses actual saved-cache loss and export failure rather than only mocked successful responses.

Observed post-review CLI proofs: importing the regression and modeling modules leaves discovery unchanged; valid warm-open sampling exits **0**; a folded-output path that is a directory exits **1** with `throughput.valid=false` and a null rate; removing actual cache entries before open exits **1** with invalid throughput; and the real CPU governor stop at step 10 exits **1** with `mlups:null`. The sampling/solver-driver failure statuses here are distinct from `perf_regression.py`'s four-way exit contract below.

[tools/perf_baseline.json](../../tools/perf_baseline.json) was migrated to the verified **after** record, schema **2**. It is specific to this machine, Python, thread settings, ABI and workload/timing contracts. Do not compare foreign or legacy records, or alias historical `open_cold` to `open_warm`. Capture a local baseline with `--baseline … --update`; a missing baseline records one without comparison. `--json` must not overwrite the baseline. The default regression requires **both >25% median increase and >1 ms absolute increase**.

The repository schema-2 baseline preserves every measured timing, but its historical Linux `getrusage` peaks are now explicitly demoted to `available=false`, with a reason and no numeric value, rather than carried forward as application-memory evidence. New Linux captures use `/proc/self/status` `VmHWM` for the current executable image; macOS retains the documented platform `getrusage` measurement. The dedicated performance-test run covering this latest RSS branch completed: **30 passed in 0.22 s**. An actual two-sample warm-open capture using the corrected helper also completed successfully.

Exit status: **0** valid pass/baseline recorded; **1** measured regression; **2** unavailable engine; **3** invalid arguments/baseline/workload, failed measurement or report write. [scripts/gate.sh](../../scripts/gate.sh) recognizes these separately; `CHUDWARE_RUN_PERF=1` opts in, `CHUDWARE_PERF_FATAL=1` makes measured regressions fatal, and **`CHUDWARE_PERF_BASELINE`** overrides the gate baseline path. It is a gate setting; direct tool invocation uses `--baseline`. Parent caller review found no additional callers requiring migration beyond the updated source-linked documentation; no legacy compatibility alias is retained.

### Independent IPC, ECAD, SHA, LBM and GL drivers

```bash
"$PY" tools/profile_ipc.py --repeats 7 --output scratch/performance-local/ipc.json
# For an original Python snapshot, add --module-file /absolute/path/daemon_bridge_before.py.

core/build-occt-linux/bin/ecad_profile --fixture dense --side 50 \
  --resolution 0.25 --iterations 3 --output scratch/performance-local/ecad-dense.json
core/build-occt-linux/bin/ecad_profile --fixture mixed --side 12 \
  --resolution 0.25 --iterations 3 --output scratch/performance-local/ecad-mixed.json
core/build-occt-linux/bin/ecad_profile --fixture boundary --side 12 \
  --resolution 0.1 --iterations 3 --output scratch/performance-local/ecad-boundary.json
core/build-occt-linux/bin/hash_profile 67108864 32
core/build-occt-linux/bin/lbm_profile --backend both --mask asymmetric \
  --nx 128 --ny 64 --nz 64 --steps 200 --iterations 3

# Requires an accessible display; inspect JSON renderer, not DISPLAY or Xvfb assumptions.
host/build-linux/bin/profile_viewport --mode both --iterations 10000 \
  --hits scratch/performance-local/viewport.csv \
  --png scratch/performance-local/id --require-memoized

# Attribution only; separate from uninstrumented parity and wall-time runs.
nsys profile --trace=cuda --force-overwrite=true \
  -o scratch/performance-local/lbm-nsys \
  core/build-occt-linux/bin/lbm_profile --backend cuda --mask asymmetric \
  --nx 128 --ny 64 --nz 64 --steps 200 --iterations 1
```

Durable sources: [profile_ipc.py](../../tools/profile_ipc.py), [ecad_profile.cpp](../../core/tools/ecad_profile.cpp), [hash_profile.cpp](../../core/tools/hash_profile.cpp), [lbm_profile.cpp](../../core/tools/lbm_profile.cpp), [profile_viewport.cpp](../../tools/profile_viewport.cpp); build targets are in the core/host CMake files. ECAD canonical output is timing-free and suitable for `cmp` against an original run. LBM defaults to strict parity; `--allow-baseline-mismatch` exists solely to retain known-defective original-path evidence and is not an acceptance mode for the corrected backend. The GL driver emits timing-free CSVs for exact old/new comparison; omit `--require-memoized` only when measuring the original implementation.

## 8. Exercised validation and qualification limits

Completed checks:

- Corrected focused CUDA simulation tests: **12 tests / 72,989 assertions passed**.
- Final scoped CTest validation: **all eight selected modules passed** (`autoroute`, `kicad_import`, `sch_engine`, `drc_geometry`, `sim`, `appearance`, `selectors`, `sha256`) in **40.03 s**, including the full **38-case simulation suite** on the CUDA-enabled build. These completed results supersede the earlier simulation failure during bring-up.
- Post-review combined `tools/test_perf_regression.py`, `tools/test_profile_modeling.py` and `services/mcp/test_daemon_bridge.py`: **70 passed in 10.51 s**, including the 38 daemon cases and real cache-loss/export-failure regressions. This supersedes the earlier 66-case, 6.50 s combined run.
- Exact 442-selector comparison; byte-identical dense PCB output and matching mixed/boundary fingerprints; CPU/GPU field/force/convergence equality and deterministic fingerprints; full GL hit CSV equality, attachment fingerprint equality, zero GL errors and memo-invalidation guards.
- Real desktop face-6 selection, visually inspected M1 edit/save/reopen with three-feature history, and bidirectional old/new verified warm-cache open/resave with zero feature execution.
- Post-review CLI reruns retained exact equality for all **442 selectors**; a **16×8×8, 30-step, two-iteration** CPU/GPU run passed parity; the hash driver matched Python `hashlib` at **15 sizes including 64 MiB**. Actual import/restoration, cache-loss, export-failure and governor-stop observations are detailed in §7; refused/failed work does not retain valid throughput.

### Full repository gate and resolved environment failures

The full `scripts/gate.sh` run completed in **4,997.39 s with exit status 1**, not a clean monolithic pass. Its retained logs are under `/tmp/chudware-gate-762081/`. Completed successful stages included:

- All ten source checks, manual regeneration, the native core build, all host build targets, and the **WASM web-client build**.
- **All 137 core CTest executables passed** in **501.08 s** on the CPU build.
- Hostd lifecycle/race/stop/recovery smokes, web parity, Linux packaging tests (**187 passed, 27 skipped**), Fin25 (**13 passed, no failures or skips**), and the real GUI M1/copilot capture scenarios.
- All Python gate units other than `services/mcp` and `tools` passed. Those two units accounted for the gate's **five failures**, diagnosed and cleared as follows.

Three `services/mcp/test_hostd_cell_workers.py` cases failed at socket connect with **`OSError: AF_UNIX path too long`**: the drain/background-completion case and both background/unknown-pin idle variants. The inherited temporary directory made the fixture's externally accessed operator socket exceed the Unix address limit. All three first passed with a short explicit pytest base directory (**3 passed in 52.15 s**); the **complete MCP suite** then passed with the same path correction: **4,017 passed, 34 skipped, 1 xfailed, 16 subtests passed in 1,343.52 s**.

Two DVC round-trip cases (`tools/flywheel/test_export.py::TestDVCRoundTrip::test_dvc_push_pull_roundtrip` and `tools/flywheel/vlm/tests/test_vlm_track.py::test_dvc_corpus_roundtrip`) failed during `dvc init`. The selected `/home/hunter/.local/bin/dvc` launcher uses system Python 3.14, whereas the test runner uses the provisioned Python 3.11 virtualenv. The suite's private `HOME` hid DVC's user-installed package: running that launcher with private `HOME` reproduced **`ModuleNotFoundError: No module named 'dvc'`**. Explicit `PYTHONUSERBASE=/home/hunter/.local` restored package discovery without exposing the real home as the test's home. The **complete tools suite** then passed: **525 passed, 1 skipped in 189.40 s**.

The complete affected-suite reruns used the following settings from the repository root; these are test-process settings, not application changes:

```bash
env TMPDIR=/tmp PYTHONUSERBASE=/home/hunter/.local \
  CHUDWARE_SIM_THREADS=4 OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 \
  CHUDWARE_HOSTD_BIN="$PWD/core/build-linux/bin/chudware-hostd" \
  CHUDWARE_LIBCHUDWARE="$PWD/core/build-linux/bin/libchudware.so" \
  .venv-mcp/bin/python -m pytest -q \
  --basetemp=/tmp/cw-perf-mcp-full services/mcp

env TMPDIR=/tmp PYTHONUSERBASE=/home/hunter/.local \
  CHUDWARE_SIM_THREADS=4 OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 \
  CHUDWARE_HOSTD_BIN="$PWD/core/build-linux/bin/chudware-hostd" \
  CHUDWARE_LIBCHUDWARE="$PWD/core/build-linux/bin/libchudware.so" \
  .venv-mcp/bin/python -m pytest -q \
  --basetemp=/tmp/cw-perf-tools-full tools
```

No application change, assertion weakening, or test exclusion was used to clear these failures. Both complete failed units are now green; the original monolithic gate remains recorded as failed and was not rerun in its entirety. Rerun durations above are validation times, not performance benchmarks.

Windows, macOS, ARM, browser/WebGL performance, other GPU architectures, installed-release packaging and customer hardware were not exercised by this performance pass. Historical/native Windows evidence elsewhere does not qualify these changes on Windows. GPU availability/fallback remains environment-dependent; sm_75 compilation is not runtime validation on a Turing card. No claim covers every model, all grid sizes, every simulation family or all product services. The result is an implemented and measured set of exact hot-path improvements, with explicitly bounded evidence and a retained reproduction path—not a blanket performance certification.
