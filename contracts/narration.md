# Narration Style Guide

Shared contract for all Chudware text-narration tools (`describe_toolpaths`, `describe_results`,
and any future narration tool). Tools that produce structured text about CAD/CAM/FEA data
**must** follow this guide instead of ad-hoc formatting.

---

## Core principle: narrate, never recompute

Every numeral in the output text **must be traceable to a named tool call or a named key
in the tool's response payload**. The narration layer is a lens over existing numbers — it
does not recompute derived values except:

- Factor of safety: `yield_mpa / max_von_mises_mpa` (explicitly labelled as such)
- Margin: `yield_mpa - max_von_mises_mpa` (explicitly labelled as such)

All other values (stress, displacement, cycle time, gouge counts, G-code bounds, feed
ranges) are copied from the source tool response and attributed with `(source: <tool>)`.

---

## Rounding conventions

| Quantity | Precision | Example |
|---|---|---|
| Linear dimensions (mm) | 3 significant figures (use `:.3g`) | `6.35mm`, `0.254mm` |
| Stresses (MPa) | 1 decimal place | `247.3 MPa` |
| Displacements (mm) | 3 decimal places | `0.012 mm` |
| Factors of safety | 2 decimal places | `1.43` |
| Percentages | 1 decimal place | `87.5%` |
| Cycle times (seconds) | 1 decimal place | `142.7s` |
| Feed rates (mm/min) | 0 decimal places (integer) | `1500 mm/min` |
| Frequencies (Hz) | 1 decimal place | `234.1 Hz` |
| G-code coordinate bounds | 3 significant figures | `X[-5.00..95.0] mm` |

---

## Adjective rule

**No adjective without a number behind it.** Forbidden patterns:

- "high stress" — replace with "stress 312.4 MPa (94% of yield)"
- "slow cycle time" — replace with "cycle 847.2s"
- "large displacement" — replace with "displacement 2.341 mm"

---

## Graceful degradation

When data is missing, say so **explicitly** — never silently omit a section.

Required phrases (use these exact patterns):

| Situation | Required output |
|---|---|
| No simulation results | `No simulation results attached — run run_fea_static or run_fea_modal first.` |
| No CAM job active | `No CAM job. Call cam_new_job first.` |
| Toolpaths not generated | `Toolpaths not yet generated — run cam_generate first.` |
| Simulation not run | `No simulation results — run cam_simulate for cycle time and gouge checks.` |
| G-code not available | `G-code: not available — run cam_post first.` |
| Max-stress location not persisted | `Max-stress node location: not stored in the study record — the study persists summary scalars only; re-run run_fea_static for the live field.` |
| No study by that name | `No study named '<name>'. Available: [<list>]` |

---

## Stable sid naming

Face references in narration text use the `sid` identifiers from `describe_part`'s face
layer — never raw face ordinals or coordinates. Example: `face f3 (planar on top)`.

When a sid cannot be resolved, state it explicitly rather than omitting:
`face location not resolved — call hover(sid) to locate`.

---

## Section order

All narration output follows this canonical section order:

1. **Header summary** — tool/job/study name, type, counts (setups, operations, DOF, etc.)
2. **Per-item details** — one block per setup/op or per result category
3. **Caveats / warnings** — convergence notes, occlusion disclaimers, missing-data notices

Tools **must not** reorder these sections even when some are empty (use the graceful
degradation phrases to fill empty sections).

---

## Source attribution

Each scalar value carries a parenthetical source tag at line end:

```
Max von Mises stress: 247.3 MPa  (source: run_fea_static)
Max displacement: 0.031 mm  (source: run_fea_static)
Mode 1: 234.1 Hz  (source: run_fea_modal)
```

The G-code digest attributes its values as `(source: cam_post G-code parse)`.

---

## Tools covered by this contract

### `describe_toolpaths` (CAM)

Data sources (in call order):

1. `cam_job_tree` → the job payload is nested under `"tree"` (`core/cam/session.cpp
   tree_of`): op list (id, name, type, tool_number, suppressed), tool library
   (number, name, type, diameter), setup hierarchy. The compact tree does NOT carry
   per-op cut parameters (stepdown/stepover/stock_to_leave) — those lines appear only
   when the tree grows them; no other read RPC exposes them today.
2. `cam_simulate` → `{"results": [SimResult...]}` keyed by `setup_id`
   (`core/cam/stock_sim.cpp SimResult::to_json`): `machined_fraction` (0..1 — format
   as a percentage, formatting is not recomputation), `remaining_volume_mm3`,
   `cycle_time_s`, `gouges`, `rapid_collisions`
3. `cam_post` → `{ok, gcode, line_count, tool_changes, est_time_s, warnings}`
   (`core/cam/post.cpp PostResult::to_json`). `line_count` / `tool_changes` /
   `est_time_s` are narrated from the post result itself; only coordinate bounds
   (X/Y/Z from G0/G1 lines) and feed range (F values) are parsed from the `gcode`
   text — a deterministic modal subset, never toolpath semantics

Output skeleton:

```
CAM job "<name>" — <N> setup(s), <N> operation(s) total
Setup "<name>" [<machine>] — <N> op(s)
  Op "<name>" [<type>]: T<num> <tool_type> ø<diam>mm
    stepdown <val>mm, stepover <val>mm, stock-to-leave <val>mm   [only if the tree carries them]
    Engage: <val>%                                               [only if the tree carries it]
  Simulation: machined <pct>%, remaining stock <val> mm3, cycle ~<sec>s  (source: cam_simulate)
  Gouges: <N>; rapid-through-stock collisions: <N>  (source: cam_simulate)

G-code digest (source: cam_post): <N> lines, bounds X[<min>..<max>] Y[<min>..<max>]
  Z[<min>..<max>] mm, feed <min>–<max> mm/min, <N> tool change(s), est. time <val>s
Audit (source: cam_audit): ok|FAILED — <N> error(s), <N> warning(s); bounds
  X[<min>..<max>] Y[<min>..<max>] Z[<min>..<max>] mm; feed <min>–<max> mm/min;
  est. <val>s
(Source: cam_job_tree, cam_simulate, cam_post, cam_audit — numbers narrated, never recomputed)
```

The `Audit` line is appended when `describe_toolpaths(audit=True)` (the default) and
the `cam_audit` RPC is reachable; on bridge error the line reads
`Audit unavailable: <err>`. All figures come from the `aicad.cam_audit/1` envelope
(`stats.bounds`, `stats.feed_range_mm_min`, `stats.est_time_s`) — never recomputed
from G-code text.

### `describe_results` (FEA/CFD)

Data sources:

1. `sim_study_list` → the doc's persisted studies (the records `_persist_study`
   writes after every solve): type, name, material (incl. `yield_mpa`),
   fixtures/loads as the ORIGINAL plain-English queries, `mesh_fineness`, `n_dof`
2. Study `results` dict: `max_von_mises_mpa`, `max_disp_mm`, `min_fos` (static);
   `freqs_hz[]`, `n_modes` (modal); `drag_n`, `lift_n`, `cd`, `reynolds`, ... (cfd_flow)
3. Sid join (best-effort): the stored fixture/load face queries are resolved against
   the live face layer (`doc_query`, the same sid namespace `describe_part` uses) to
   name the fixed and loaded faces. The per-node stress field is NOT persisted in the
   study record, so the max-stress node cannot be located from stored data — the
   output says so explicitly rather than guessing

Output skeleton:

```
FEA results — "<study_name>" (<type>)
Material: <name> (yield <val> MPa, source: run_fea_static material record)

Static stress:
  Max von Mises stress: <val> MPa  (source: run_fea_static)
  Fixed at: face <sid> (<surf> on <side>)  (sid join of the stored fixture queries)
  Load applied on: face <sid> (<surf> on <side>)  (sid join of the stored load queries)
  Max-stress node location: not stored in the study record — the study persists
  summary scalars only; re-run run_fea_static for the live field.
  Max displacement: <val> mm  (source: run_fea_static)
  Min factor of safety: <val>  (yield / max stress; >1 means holds)
  Margin: <val> MPa above/below yield
  Mesh: <n_dof> DOF  (mesh_fineness <val>, tet10 elements)

Modal frequencies (first <N> modes):
  Mode 1: <val> Hz  (source: run_fea_modal)
  ...

Convergence note: tet10 (2nd-order) elements used; re-run with finer mesh
(mesh_fineness → 0.8) to verify convergence for final design.
(Source: sim_study_list — numbers narrated from the stored study, never recomputed)
```

---

### `describe_assembly` (assemblies / multi-body parts)

Data sources (in call order):

1. `asm_world_bodies` → `{"bodies":[{name, component, bbox:{min,max,size}}], multibody_part}`
   (`core/doc/assembly.h AsmBodyOut`): one entry per placed component / body.
2. `doc_solve_mates` → `{"revision", "status": <rebuild.doc_status>}` — top-level summary
   string; DOF per component is not exposed via a read RPC; output says so explicitly.
3. `eval_interference` → `{summary, interferences:[{a,b,volume}], clearances:[{a,b,distance}]}`
   (`check_interference` shape); volume in mm^3, distance in mm.

Graceful degradation phrases for this tool:

| Situation | Required output |
|---|---|
| No bodies | `No bodies found in this document — add components (assembly) or features (part) first.` |
| DOF not readable | `DOF detail not available via read RPC — call solve_mates for the live result` |
| Interference/clearance unavailable | `Fit/clearance: not available for this document` |

Output skeleton:

```
Assembly "<doc_id>" — <N> component(s) / bod(y/ies) [<kind>]

Component placement (world frame):
  <name>: <W> × <H> × <D> mm, origin [<x>, <y>, <z>]  (source: asm_world_bodies)
  ...

Mate solve: <status string>  (source: doc_solve_mates)

FIT REPORT: <summary>
  INTERFERENCE  <a> <-> <b>: <vol> mm^3 overlap
  clearance     <a> <-> <b>: <dist> mm gap
  -> [actionable fix advice — only when interferences exist]

(Source: asm_world_bodies, eval_interference, doc_solve_mates — numbers narrated, never recomputed)
```

### `describe_board` (ECAD schematic / PCB)

Promotes `ecad_status` into the GSL narration layer. Reads directly from the ECAD Python
engine modules — no extra RPC round-trip.

Data sources:

1. `engine.ecad.ecad_session.get_session` → session kind (`sch`|`pcb`), doc dict
2. `engine.ecad.netlist.build_netlist` → `{components:[{ref,libid,value,footprint,pins}], nets:{name:[members]}}`
3. `engine.ecad.erc.run_erc` → list of violation dicts `{severity, rule, ...}` (schematic)
4. `engine.ecad.drc.run_drc` → violation list or `{violations:[...]}` (PCB)

Graceful degradation phrases:

| Situation | Required output |
|---|---|
| No active ECAD document | `No active ECAD document: <error> — open one with ecad_new_schematic or ecad_new_pcb first.` |
| ECAD engine not available | `ECAD engine not available — open a schematic or PCB first (ecad_new_schematic / ecad_new_pcb)` |
| ERC/DRC not available | `ERC: not available` / `DRC: not available` |

Output skeleton (schematic):

```
Board "<name>" — schematic
  <N> component(s), <N> net(s)  (source: engine.ecad.netlist)

Components:
  <ref>: <value> (<libid>[, fp=<footprint>])
  ...

Nets:
  <net_name> (<N> pin(s)): <ref>.<pin>, ...
  ...

ERC: <N> error(s), <N> warning(s)  (source: engine.ecad.erc)

Unconnected pins (<N>): <list>  [only when unconnected exist]

Caveats:
  - <caveat>  [only when problems present]

(Source: engine.ecad.netlist, engine.ecad.erc — numbers narrated, never recomputed)
```

### `describe_project` (DAG run)

Narrates a caller-supplied DAG run dict; no engine RPC. The run dict is passed in by the
caller (DAG driver or dag_status()) — this tool is a formatter, not a reader.

Data sources: `run` parameter dict matching `contracts/dag_schema.md`.

Fields read: `run.id`, `run.name`, `run.nodes[]{id,title,name,status,owner,blocked_reason}`,
`run.budget{spent,cap,tokens_used,tokens_cap}`, `run.open_questions[]{id,task,question,text}`.

Graceful degradation phrases:

| Situation | Required output |
|---|---|
| No run supplied (run=None) | `No DAG run data supplied — pass run=<dag_run_dict> from dag_status() once services/dag is available...` |
| Empty nodes list | `No nodes in this run — the DAG may not have started yet.` |
| No open questions | `No open questions.` |

Output skeleton:

```
Project "<name>" (DAG run <id>)
  <N> node(s) total: <N> done/review, <N> in-progress, <N> blocked, <N> pending
  Budget: <spent> / <cap> tokens used (<pct>%)  (source: dag_run.budget)

Completed nodes:
  [done] <id>: <title>

In progress:
  [doing] <id>: <title>  (owner: <owner>)

Blocked:
  [blocked] <id>: <title>  reason: <reason>

Open questions (<N>):
  Q (<task_id>): <question text>

(Source: dag_run dict — numbers narrated from the DAG run record, never recomputed)
```

---

## Shared fit/clearance formatter

`_format_fit(fit)` lives in `services/mcp/gsl_tools.py` — the single canonical home.
`services/agent/loop.py` imports it from there. The input shape is:
`{summary, interferences:[{a,b,volume}], clearances:[{a,b,distance}]}`.

---

## Adding a new narration tool

1. Name it `describe_<domain>` (lowercase, underscore-separated).
2. List every data-source tool call at the top of its docstring with the key names it reads.
3. Follow the section order: header → per-item → caveats.
4. Apply the rounding table above.
5. Apply the graceful-degradation phrases.
6. End the output with a `(Source: ...)` attribution line.
7. Add a test in `test_gsl_tools.py` with at least: (a) graceful-no-data case, and (b)
   happy-path case asserting key slot presence (MPa, mm, Hz, etc.) and at least one
   numeral via regex.

## 2026-09-12 — Superseding collision-only assembly narration contract

This section supersedes the `describe_assembly` section above wherever it describes
gap distances, graceful fit-unavailable success, or numbers being “never recomputed.”
Those exact-gap/no-recompute claims and the historical output skeleton are obsolete
for assembly narration. Other narration tools retain their own data-source contracts.

`describe_assembly` performs these native operations in order:

1. `asm_world_bodies` queries the current world-frame placement snapshot.
2. `doc_solve_mates` performs a live mate solve and may update placements. The placement
   rows remain the pre-solve snapshot; call `solve_mates` first when those rows must
   reflect the solved pose. Non-connection mate errors are narrated explicitly.
3. `eval_interference` evaluates current geometry with `clearance_mode="threshold"`,
   `min_clearance_mm=0`, and `include_coincident=false`. This collision-only request
   retains exact Boolean Common overlap volumes and complete geometry/pair-failure
   accounting without requesting exact minimum-distance calculations for close pairs.

The reply retains `bodies` and adds the unmodified native report as `fit`, including
`complete`, `pairs_checked`, `pairs_total`, `pair_failures`, and `geometry_failures`.
Top-level `ok` means a complete fit report was available for narration, not that the
bodies do not clash: a complete report of a real clash has `ok=true`, `fit.ok=false`,
and `collision_free=false`. `collision_free=true` requires complete, failure-free
coverage of at least one pair and a passing native fit result. One-body reports can
be narrated successfully but are explicitly not collision-free qualifications.
Empty geometry, missing coverage metadata, incomplete checks, and fit errors do not
produce passing descriptions. Known overlaps and native failure details remain visible
even when a check is incomplete. Engine connection loss remains a structured failure,
including its code/detail, rather than becoming advisory “not available” text.

`_format_fit` is implemented in `services/mcp/fit_narration.py` and imported by
`gsl_tools.py`. It formats `distance_lower_bound_mm` as a **lower bound (not measured)**.
Absent exact distances remain unmeasured, never numeric zero. A genuine zero lower bound
may be printed as such; it is not an exact zero gap. Assembly narration explicitly says
that no minimum measured gap is available and does not report an exact tightest gap.
Its source attribution distinguishes the pre-solve placement snapshot, live mate solve,
and live threshold classification/overlap-volume evaluation.

## Current implementation pointers

The online runtime is the Chudcode session host through `services/agent/worker_rpc.py`;
the retained Python provider loop is `services/agent/agent_turn.py`, not the removed
`services.agent.loop`. This updates implementation references only, not narration payloads.
