# simsuite build DAG + optimization plan

The monolithic subagent DAG that built this benchmark/test suite, and the
solver-optimization plan it teed up. Recorded 2026-07-12 so the structure
survives even if the session transcript is pruned.

- Live workflow script (may be GC'd): `…/workflows/scripts/sim-test-suite-dag-wf_8ac0a34f-f49.js`
- Run: `wf_8ac0a34f-f49` — 26 agents, ~37 min, 3.0M subagent tokens, 893 tool calls.
- Harness contract: [README.md](README.md) · resource guard: [resguard.py](resguard.py)
  · engine-side governor: `core/util/resource_governor.{h,cpp}`

## Orchestration principle

The main loop (me) owns **orchestration + the in-the-weeds solver optimization**;
subagents do the **bounded, well-scoped test writing** (cheap, parallelizable).
One monolithic DAG fans out every solver family at once, each domain's tests
independently re-verified by a separate skeptic agent, with profiler expansion
and read-only optimization scans running concurrently, then a serial timing
baseline once the box is quiet.

```
                          ┌─────────────────────────────────────────────┐
                          │  PHASE 1: SUITES  (9 domain builders,        │
                          │  pipelined — each verifies as soon as ready) │
                          └─────────────────────────────────────────────┘
  fea_static ─┐
  fea_modal_freq ─┐                    each suite(d) ──▶ verify(d)   [Phase 2]
  fea_thermal ─┐  │                    (independent re-run of the exact
  fea_ccx_advanced │  pipeline(), no   suite cmd + bench smoke, spot-read
  cfd ─┤           │  barrier: domain  for vacuous/weakened asserts;
  ecad_sim ─┤      │  A verifies while  cheap model, low effort)
  em_wave ─┤       │  domain B still
  ecad_packs ─┤    │  building
  multiphysics_misc┘
        │
        ├───────────────▶  PHASE 3: PROFILER  (1 agent, isolated git worktree,
        │                  MAY build C++; adds per-phase mesh/assemble/solve/
        │                  post timers, surfaces via prof_phases RPC)   ── concurrent
        │
        ├───────────────▶  PHASE 4: OPTSCAN  (6 read-only agents: mesher,
        │                  fea_solve, lbm, spice, em_foam, cross_cutting;
        │                  obvious waste + creative cycle-savers)       ── concurrent
        │
        └── after all suites ▶ PHASE 5: BASELINE  (1 agent, serial full
                               run_all.py once box is idle → authoritative timings)
```

`pipeline(DOMAINS, suite, verify)` was chosen over a `parallel` barrier so a
fast domain's verifier starts immediately instead of waiting for the slowest
builder. `parallel` (barrier) was used only inside OptScan where all findings
are collected together.

## Phase 1 — domain suites (9 builders)

Every builder: orient via graphify → read `AGENTS.md` + `work/context/core-sim.md`
+ its domain context pack → write regression tests in the house style/location
of that layer (engine module = `main()`+asserts; `services/mcp` = pytest) →
write `benchmarks/simsuite/<domain>_bench.py` (README contract, `quick` < 60 s)
→ fix real bugs *scoped to its own files*, report cross-file bugs as findings →
run engine in-process (`CHUDWARE_ENGINE_DAEMON=0`) → return structured JSON.

| domain | coverage | analytic references |
|---|---|---|
| `fea_static` | 8–12 diverse parts (cantilever, plate+hole, annulus, L-bracket, shelled box, revolved, lofted, swept, multi-feature); tet4/tet10/hex × fineness sweeps → accuracy-vs-time frontier | Euler-Bernoulli tip deflection, Kirsch Kt≈3, Lamé hoop/radial |
| `fea_modal_freq` | modal natural freqs; free-free 6 rigid-body modes; frequency response via ccx path | Euler-Bernoulli beam modes |
| `fea_thermal` | steady conduction + fin/convection; transient probe | 1-D slab flux, fin theory, lumped-capacitance / semi-infinite solid |
| `fea_ccx_advanced` | CalculiX buckling, contact, fatigue; bad-`.inp`/missing-binary/timeout handling | Euler Pcr, Hertz contact, Basquin S-N |
| `cfd` | LBM channel/bluff-body + convergence curve; OpenFOAM case-gen (skip-gated) | Poiseuille profile, mass conservation |
| `ecad_sim` | RC/RLC/diode/MOSFET through the sim stack; `ecad_new_schematic→wire→ecad_simulate`; orchestrator sim (no live LLM); C++↔Python SPICE parity; ngspice if present | RC τ, RLC resonance, MOSFET DC transfer |
| `em_wave` | analytic (waveguide TE10 cutoff, microstrip Z0); BuiltinFdfdAdapter convergence; openEMS-absent clean degrade | f_c = c/(2a), closed-form Z0 |
| `ecad_packs` | one reference board → PDN IR-drop, SI, single/diff impedance, hostcheck, fpga, thermal/airflow handoff+verify, power budget, heatsink, stackup | microstrip Z0, hand-computed trace R |
| `multiphysics_misc` | motion (pendulum/free-fall), piping (Darcy-Weisbach), hydraulic sizing, structural beam, cross-sim handoff seams | 2π√(L/g), Darcy-Weisbach, F=P·A, beam max-moment |

Priority calls: `ecad_sim` + `em_wave` were previously untested → highest effort,
"test **and** fix" briefs. `fea_static` is the flagship diverse-geometry
accuracy/timing case the request centered on.

## Phase 2 — verifiers (1 per suite, pipelined)

Independent agent re-runs each suite's exact command + bench smoke fresh, compares
counts/exit codes against the builder's claims, spot-reads the two largest new
test files for vacuous asserts or weakened existing assertions. Cheap model, low
effort. No fixing — verify only. (This caught the `fea_thermal` count inflation
and the `cfd` skip-counted-as-fail harness bug.)

## Phase 3 — profiler expansion (worktree, may build C++)

Per-study phase timers (`core/prof/phases.{h,cpp}`, RAII `StudyScope`/`PhaseScope`)
across mesher/FEA/LBM/flow/overlay, surfaced by the `prof_phases` RPC
(`engine_profile action='phases'`). Release build mandatory. Integrated to master
in `b78a3fc3`; merged alongside the resource governor.

## Phase 4 — optimization scans (6 read-only agents)

Targets: `mesher`, `fea_solve`, `lbm`, `spice`, `em_foam`, `cross_cutting`.
Each hunts (a) obvious waste and (b) creative savings (geometry/netlist-hash
caching, factorization/warm-start reuse, adaptive tolerances, lazy artifacts,
batched external-solver calls), every finding with file:line evidence, gain
estimate, risk, confidence. Known prior art excluded so nothing shipped is
re-proposed. Output: **76 findings (20 creative)** → the backlog below.

## Phase 5 — serial baseline

Full `run_all.py` once the fleet is idle → authoritative per-case timings +
accuracy flags → `benchmarks/simsuite/results/<UTC>/REPORT.md`.

## Resource discipline (added mid-build, applies everywhere)

- `resguard.py` (test-side) + `core/util/resource_governor.*` (engine-side):
  fraction-of-host memory/CPU budgets, process-tree RSS watchdog, graceful
  degrade (coarsen mesh / shrink CFD grid / clean refusal) instead of OOM.
  `SIMSUITE_*` / `CHUDWARE_*` env knobs; ports to Windows/other hardware unchanged.
  See [[hunter-hardware-limits]].

---

## Outcome

All 9 domains landed green + verified (commits `201f733b`, `7b70868a`,
`aa9becb2`, `33e43faf`, `4dc52e89`); governor `14af7756`; profiler `b78a3fc3`.
ECAD-sim (7 fixes) + EM-wave (1 fix) now tested & working.

## Solver phase — SHIPPED 2026-07-12 (all C1–C4 + P1–P5)

Correctness (each with a regression witness in the bench + tests):

| id | fix | commit | proof |
|---|---|---|---|
| C1 | pressure fan normals oriented outward vs element centroid | `f7d51876` | native-pressure bar 585% → 0.37% rel_err; dynamic xfail → PASS |
| C3 | steady thermal applies each flux load independently | `d2a922fe` | same-face double-flux exact; node-wise superposition 2.5e-13 |
| C2 | analytic curved-face facesets (surface:{cylinder/cone/sphere/torus} from doc_face_points; exact distance-to-surface in the solver) | `d8f961df` | cylinder barrel 1516/1516 nodes, zero bleed; native curved pressure matches Lamé to 2.0% (`annulus_lame_native_tet10`) |
| C4 | free-free modal resolves the full 6-mode rigid cluster in the engine (over-request + 3k+1 subspace + verified retry) | `b62d1694` | n_modes=8 tet10 beam: 6 rigid + 2 elastic exactly |

Performance (all bit-identical / parity-proven; measured on the 7600X3D):

| id | change | commit | measured |
|---|---|---|---|
| P1 | deterministic parallel FEA assembly + stress recovery (pre-assigned triplet slots; parallel compute + in-order scatter) | `547d9b74` | assembly ~4.6×; results byte-identical |
| P2 | CHOLMOD supernodal LLT for modal shift-invert + thermal steady/transient (+ shared solver_threads.h cap) | `b5b528d3` | modal 26.4s → 2.0s (13×); thermal 0.67s → 0.25s (2.7×) @54k DOF |
| P3 | LBM fused macros/feq/collide (feq buffer deleted), collapse(2) stream, link-list bounce-back, precomputed solid list + inlet row | `e6aca0de` | cfd bench 109s → 39.5s (2.8×); metrics bit-identical |
| P4 | STEP parsed once per mesh_step (was up to 4×); shared_ptr mesh cache (no deep copy on HIT); sim_mesh RPC cached | `e5822ae3` | repeat mesh 0.982s → 0.019s (52×) |
| P5 | degenerate-faceset short-circuits + lazy weight arrays + planar fast-reject in nodes_in_faceset | `52d5fc10` | oracle-loads Lamé solve_s 1.19s → 0.80s |

Still on the backlog (not blocking): factorization reuse across load cases
(needs a multi-load-case study API first); spatial-hash sample lookup for very
fine meshes with huge sample clouds; remaining opt-scan findings in
`benchmarks/simsuite/results/*/REPORT.md`. The pre-existing ctest `mirror`
failure (UnifySameDomain seam merge) is tracked separately — unrelated to sim.
