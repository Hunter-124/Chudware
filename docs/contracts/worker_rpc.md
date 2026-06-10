# Contract: Worker RPC — protocol 2

> Wire format is unchanged from today: newline-delimited JSON over stdio.
> Request `{"id": n, "method": str, "params": {…}}` →
> zero or more event lines `{"id": n, "event": kind, "data": {…}}` →
> final `{"id": n, "ok": true, "result": {…}}` or `{"id": n, "ok": false, "error": {code, message, detail}}`.
> (Legacy methods that returned `error` as a plain string keep doing so; all NEW methods use the
> structured error envelope of doc_ops.md §5.)

## 1. Handshake & versioning

`ping` → `{"pong": true, "build123d": "...", "protocol": 2}`.
The host refuses to run against a worker whose `protocol` major differs from what it was built
for, and shows the repair-runtime flow. Workers tolerate unknown OPTIONAL params (ignore).

## 2. Doc worker (`engine/worker.py`) — method table

### Legacy (unchanged, frozen)
`ping` · `run_model {code, name?}` · `agent_design {request, name?, session_id?, reset?,
max_iters?, plan?}` · `get_prompts` · `shutdown`

### Documents
| method | params → result |
|---|---|
| `doc_new` | `{kind: "part"\|"assembly"}` → ops-response (doc_ops §1) + `doc_id` |
| `doc_open` | `{path}` → ops-response + `doc_id` + `warnings:[…]` |
| `doc_save` | `{doc_id, path?}` → `{path}` (`.step+` writer; path omitted = current) |
| `doc_close` | `{doc_id}` → `{closed: true}` (discards unsaved state) |
| `doc_list` | `{}` → `{docs:[{doc_id, name, kind, path, revision, dirty}]}` |
| `doc_apply_ops` | doc_ops §1 envelope → ops-response |
| `doc_undo` / `doc_redo` | `{doc_id}` → ops-response |
| `doc_tree` | `{doc_id}` → `{revision, tree}` (no rebuild) |
| `doc_rebuild` | `{doc_id, from?: feature_id}` → ops-response (forced) |
| `doc_resolve_pick` | `{doc_id, revision, body_id, kind: "face"\|"edge"\|"vertex"\|"body", topo_id}` → `{selector}` (errors `stale_revision` if revision moved) |

### Sketch interaction (M2)
| method | params → result |
|---|---|
| `sketch_enter` | `{doc_id, sketch}` → `{entities:[{id, …solved params}], statuses:{e1:"full",…}, dof:int, plane_frame:[16]}` |
| `sketch_solve` | `{doc_id, sketch}` → same as `sketch_enter` |
| `sketch_drag` | `{doc_id, sketch, handle, xy:[x,y]}` → `{entities, statuses}` — transient, warm-started, does NOT commit or bump revision |
| `sketch_drag_commit` | `{doc_id, sketch}` → ops-response (persists final positions as one undoable batch) |
| `sketch_exit` | `{doc_id, sketch}` → ops-response (rebuilds downstream) |

### Evaluate (M5; overlay-producing methods return `{aicm_path}` with `SCLR` chunks)
`eval_measure {doc_id, refs:[Selector,…]}` → `{kind, distance?, angle?, length?, area?, radius?,
points?:[…]} `· `eval_mass {doc_id, body?}` → `{volume_mm3, mass_g, com:[3], inertia:[9],
material}` · `eval_section_props {doc_id, plane: Selector|datum-id}` · `eval_interference
{doc_id}` → fit-report shape (kernel `_fit_report`) · `eval_draft {doc_id, pull:[3], angle_deg}`
· `eval_thickness {doc_id}` · `eval_curvature {doc_id, kind: "gaussian"\|"mean"\|"max"}` ·
`eval_check {doc_id}` → `{issues:[{kind, severity, text, ref?}]}` · `eval_symmetry {doc_id,
plane}` → `{symmetric: bool, deviation_mm3}` · `eval_perf {doc_id}` → `{features:[{id, name, ms}]}`

### Formats (M8; minimal `doc_save`/`doc_open` for `.step+` exist from M1)
`import_file {path, into?: doc_id}` → ops-response (adds `import` feature(s)) ·
`export_file {doc_id, path, format: "step"|"stl"|"brep"|"iges"|"3mf"}` → `{path}`

### AI (M9)
`agent_run_v2 {request, doc_id, options?:{max_stage_attempts?, plan?, roles_override?}}`
→ streams events.md orchestrator events → `{run_id, status, stop_reason, revision}` ·
`get_prompts` (v2 result: `{prompts:[{role, path, override_path, text, overridden}]}`) ·
`agent_cancel {run_id}` → best-effort stop at the next stage boundary.

## 3. Sim worker (`engine/sim/worker.py`, separate process, spawned on demand)

Same wire protocol. One job in flight; `job_id` correlates events.

| method | params → result |
|---|---|
| `ping` | → `{pong: true, protocol: 2, sim: true}` |
| `sim_mesh` | `{step_path, density: 0..1}` → `{mesh_path, nodes, elems, char_len_mm}` |
| `sim_run_static` | `{study}` (study JSON: material key + E/ν/ρ/yield, fixtures:[Selector→node sets prepared by doc worker as face point clouds], loads:[{faces, type: force\|pressure, vector\|magnitude}], mesh_path) → `{results_npz, overlay_aicm, summary:{max_von_mises_mpa, max_disp_mm, min_fos}}` |
| `sim_run_modal` | `{study, n_modes: 6}` → `{freqs_hz:[…], results_npz, overlay_aicm}` |
| `sim_run_flow` | `{stl_path, direction:[3], speed_mps, preset: "coarse"\|"medium"\|"fine"\|"xfine"}` → `{drag_n, lift_n, cd, frontal_area_mm2, reynolds_note, slices_aicm, lines_aicm, trace:[{step, drag}]}` |
| `sim_cancel` | `{job_id}` → `{cancelled: bool}` |
| `shutdown` | |

All `sim_run_*`/`sim_mesh` stream `sim_progress` events (events.md). The sim worker receives
geometry as FILES (STEP/STL paths) + study JSON — it never touches live document state, so a
crash cannot corrupt a session (host restarts it on demand).

## 4. Concurrency & timeouts

- Doc worker: requests are serialized by the host (existing mutex). Doc/sketch calls must stay
  interactive (<100 ms typical; rebuilds stream `doc_progress` past 300 ms).
- Sim worker: one job; host may poll `ping` concurrently is NOT supported — host tracks job state
  from events. Host-side timeout: none for sim (cancel is explicit), 120 s default elsewhere.
- The host owns process lifecycle: doc worker at startup (existing flow), sim worker lazily,
  restart on crash with a console notice.
