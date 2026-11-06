# Contract: ECAD Worker RPC — v1

> ECAD methods on the existing JSON-RPC worker (`core/api/libchudware.cpp`, protocol 2, newline-delimited
> JSON over stdio). Registered by appending `core/ecad/session.cpp`'s `METHODS` dict into the worker's
> `_METHODS` (single fenced append — see `ECAD_PLAN §6`). Error envelope identical to `worker_rpc.md`:
> `{ "code", "message", "detail" }`. Long simulations run on the **sim worker** (`sim_*`).

## 1. Document methods (doc worker)

| method | params → result |
|---|---|
| `ecad_new` | `{kind:"sch"|"pcb", name?}` → ops-response + `doc_id` |
| `ecad_open` | `{path}` → ops-response + `doc_id` + `warnings` (.ecad+ or .kicad_sch/.kicad_pcb) |
| `ecad_save` | `{doc_id, path?}` → `{path}` (writes `.ecad+`) |
| `ecad_close` | `{doc_id}` → `{closed:true}` |
| `ecad_apply_ops` | `{doc_id, base_revision, ops}` → ops-response (see `ecad_ops.md §1`) |
| `ecad_rebuild` | `{doc_id}` → ops-response (force netlist/ERC/DRC recompute) |
| `ecad_tree` | `{doc_id}` → `{revision, kind, tree, compile}` (cached, no rebuild) |
| `ecad_swap_doc` | `{doc_id, doc, base_revision?, warnings?}` → ops-response (wholesale doc replacement: validate → undo snapshot → swap → revision+1 → compile; `stale_revision` on mismatch) |
| `ecad_undo` / `ecad_redo` | `{doc_id}` → ops-response |
| `ecad_resolve_pick` | `{doc_id, layer?, xy, kinds?}` → `{selector}` (canvas pick → typed ref: pin/pad/track/via/net) |
| `ecad_describe` | `{doc_id, budget?}` → compact text/JSON design summary for the AI (see `sim.md`/tools) |
| `ecad_export` | `{doc_id, kind:"gerber|drill|bom|cpl|spice|netlist|kicad", path, opts?}` → `{paths:[...]}` |
| `ecad_to_3d` | `{doc_id, into_doc_id?, opts?}` → `{step_path, part_doc_id}` (the 3D bridge; see `fab_outputs.md §5`) |

## 2. Simulation methods

| method | params → result |
|---|---|
| `ecad_simulate` | `{doc_id, study, sim_params?, job_id?}` → `{ok, results, log, ms, cancelled?}` |
| `sim_spice` | `{circuit, study, job_id?}` → same result shape, no document session |
| `sim_cancel` | `{job_id}` → `{ok, cancelled, job_id}` |

`study` schema and `results` shape are in `sim.md`.

The solver is the native MNA engine in `core/sim/spice.cpp`, and it runs **in-process, synchronously,
inside the doc worker** — there is no isolated sim process. A `kill -9` crash-isolation guarantee
therefore does NOT hold for circuit simulation, unlike gmsh/FEA. Do not write a test asserting it.

Cancellation is cooperative, not preemptive. Pass a caller-chosen `job_id`; the solver polls the
shared token at every unbounded loop head (transient timesteps, Newton iterations, AC/DC/temperature
sweeps, Monte-Carlo runs), so `sim_cancel {job_id}` stops a run within well under a second. A
cancelled run returns `ok:false, cancelled:true`, carrying whatever partial results were already
computed — partial data is never presented as a completed run. Omitting `job_id` is allowed and
costs nothing, but the run is then uncancellable: a large transient can legitimately reach millions
of iterations and a Monte-Carlo study tens of thousands of full re-analyses, so any caller that can
be asked to stop MUST pass one.

There are no progress events. A caller that needs responsiveness runs the call off its UI thread and
cancels by `job_id`; that is what the GUI's Simulate command does.

> Superseded declaration: this section previously described an isolated `sim_run {…, study}` worker
> emitting `{event:"sim_progress"}` and a `sim_cancel {run_id}`. No such method was ever dispatched
> — `run_id` appears nowhere in the sim path, and the only `sim_run` in the tree is the unrelated
> FEA job family in `services/mcp/cad_sim_tools.py`, which is genuinely process-isolated and
> PID-killable but explicitly refuses circuit analyses. The rows above are what actually ships.

## 3. Compile-on-apply

`ecad_apply_ops` runs the relevant compile (sch → netlist+ERC; pcb → DRC+ratsnest) **synchronously**
for fast docs (target ≤50 ms typical) and returns it in `compile`. If a compile exceeds a budget it
returns `stats.partial=true` and the GUI requests `ecad_rebuild` async. Caching mirrors the CAD
compiler: keyed on `(revision, op-touched object set)` so a single-symbol edit re-netlists cheaply.

## 4. MCP bridge

`services/mcp/ecad_tools.py` calls the kernel/engine **directly** (not via the worker socket) — same
pattern as the existing `services/mcp/server.py` `run_model`. Worker RPC is for the C++ host; MCP is
for external/agent callers. Both share the same `engine/ecad` core, so behavior is identical.

## 5. v1.1 additions (additive — no v1 method changed shape incompatibly)

| method | params → result |
|---|---|
| `ecad_resolve_pick` | **now served.** `{doc_id, xy, kinds?, tol?, layer?}` → `{selector}` or `{selector: null}`. Schematic priority: pin > point entities (junction/no_connect/power/label/bus_entry/text/sheet_symbol) > symbol body > polylines (wire/bus/graphic). Board priority: pad > via > track > zone > footprint. `tol` defaults 1.27 mm; a pin selector carries the resolved `net` |
| `ecad_design` | **now served natively.** `{request, name?}` → ops-response + `doc_id` + `recipe_used` + `design` + `study?` + `ok` + `erc_errors` + `warnings`. Picks the closest deterministic recipe (`ecad/recipes.h`) and **compiles it into a real schematic document**: symbols placed on a 2.54 mm grid from `symlib`, a pin stub per connection, power ports for supply nets and global labels for signal nets, plus a title block. `ok` is false when any component/pin failed to resolve or ERC reports an error — a partially wired sheet never reports success. This replaces the Python-only orchestrator RPC the GUI used to call and which nothing served |
| `ecad_symbols` | `{query?, limit?}` → `{ok, symbols, count[, total]}` (see `library.md §6`) |
| `ecad_load_symbol_library` | `{path}` → `{ok, added, warnings}` (`.kicad_sym`) |

### 5.1 Changed v1 methods (backward compatible)

- `ecad_describe {doc_id, budget?}` — `budget` is now **honored** (default 4000 chars). The result
  gains `compiled`, `counts` (every entity kind), `sheets`, `net_table` + `net_table_truncated`,
  and `erc`/`drc` objects carrying `{available, errors, warnings, top[]}`. The v1 scalar keys
  (`symbols`, `wires`, `nets`, `erc_violations`, …) are still emitted. **`available:false` means
  the check could not run — a caller must never read it as "clean".**
- `ecad_export {doc_id|board|doc, kind, path?, opts?}` — `path` is now **honored** and the result
  gains `paths: [written…]` and `kind`. `files` is unchanged so remote/MCP callers can keep
  writing the text themselves. **`paths` is empty when nothing was written**; a caller must report
  from `paths`, never from the mere absence of an error. New schematic kinds `netlist`, `bom`,
  `spice`, `kicad`, `svg`, `pdf` join the pcb kinds `gerber`, `drill`, `cpl`, `bom`
  (see `fab_outputs.md §7`).
- `ecad_save {doc_id, path?}` → `{path, format:"ecad+", source_path?}`. A session opened from a
  `.kicad_sch`/`.kicad_pcb` records that file as `source_path` and targets the sibling
  `<stem>.ecad+` for a pathless Save; an explicit attempt to write the container **over** the
  imported source is refused with `io_error`. Previously a pathless Save silently replaced the
  user's KiCad file with ZIP bytes.
- `ecad_open {path}` — a foreign import now returns a warning naming the native save target.

### 5.2 Closed divergences

`sim_run` / `sim_cancel {run_id}` are **gone**. They declared an isolated circuit-sim worker with
streamed progress events that was never built. §2 now declares what ships: in-process
`ecad_simulate` / `sim_spice`, cooperatively cancellable via `sim_cancel {job_id}`.

`ecad_to_3d` now honors `into_doc_id` and returns `part_doc_id` as §1 always promised. It mints (or
appends to) the part document server-side, so a re-export updates the existing 3D tab instead of
spawning another anonymous one. If the STEP writes but the import fails, `part_doc_id` is `null`
and the reason is in `warnings` — the export is never reported as a failure it wasn't, and never as
a success that produced no document.

## 6. KiCad integration — contract revision v2

This revision supersedes conflicting persistence, export and check declarations above;
the worker wire protocol remains 2. KiCad project files are the document of record.
Legacy `.ecad+`, `.ecad` and `.json` open for one-way migration; saves write KiCad.
All other RPCs retain their existing contracts.

| method | params → result |
|---|---|
| `ecad_new` | `{kind:"sch"\|"pcb", name?, path?}` → ops-response + `doc_id`, `path` (bound file or `""`). With a path, writes the KiCad file and creates its sibling `.kicad_pro` if absent. |
| `ecad_new_project` | `{dir, name}` → `{project:"<dir>/<name>.kicad_pro", sch:<ops-response+doc_id>, pcb:<ops-response+doc_id>}`; creates project, schematic and board files. |
| `ecad_open` | `{path}` for `.kicad_sch`, `.kicad_pcb`, `.ecad+`, `.ecad`, or `.json` → ops-response + `doc_id`, `path`, `warnings`; legacy sessions are unbound (`path:""`). |
| `ecad_open_project` | `{path}` for a `.kicad_pro` or project file → `{project, sch:ops-response\|null, pcb:ops-response\|null}`. |
| `ecad_save` | `{doc_id, path?}` → `{path, format:"kicad", saved_revision}`. Unbound without path: `needs_path`; target locked by KiCad `~<file>.lck`: `kicad_locked`; bound file changed since load/save: `external_change`. Recovery snapshots (`recovery:true`) write to the recovery location, never the user's file. |
| `ecad_reload` | `{doc_id, discard?:bool}` → ops-response; re-imports the bound file, refusing `dirty` on unsaved edits unless `discard:true`. |
| `ecad_tree` | Existing shape plus `path`, `file_changed:bool`, `kicad_locked:bool`; a changed clean bound session auto-reloads. |
| `ecad_kicad_status` | `{}` → `{found, version, cli, kicad, eeschema, pcbnew, symbol_dir, footprint_dir, model_dir, libraries:{symbol:n, footprint:n}}`. |
| `ecad_kicad_check` | `{doc_id}` → `{ok, kind:"erc"\|"drc", kicad_version, violations:[{severity, type, description, items:[{description, uuid, xy:[x,y]}]}], unconnected, parity, counts:{error,warning}}`. Coordinates are document +y-up mm; DRC refills zones without saving and uses schematic parity when a sibling schematic exists. |
| `ecad_export` | `{doc_id, kind, path?, opts?}` → `{kind, paths:[...], files:[...], warnings?}`. PCB kinds: `gerber`, `drill`, `cpl`, `bom`, `step`, `odb`, `ipc2581`, `svg`, `pdf`, `render` (PNG); schematic: `netlist`, `bom`, `spice`, `svg`, `pdf`. Netlist defaults to kicadsexpr; `opts.format` accepts kicad-cli netlist formats including `spice`. `paths` is empty without `path`. |
| `ecad_to_3d` | `{doc_id, into_doc_id?, opts?}` → `{step_path, part_doc_id}`; STEP from KiCad and imported via `doc_import_file`. |
| `ecad_symbols` | Existing shape, including installed KiCad library results. |
| `ecad_footprints` | `{query?, limit?}` → `{ok, footprints:[{fpid,description,pad_count}], count}`. |
| `ecad_kicad_libraries` | `{}` → `{symbol:[{nickname,uri,scope}], footprint:[{nickname,uri,scope}]}`. |

Checks and exports use `kicad-cli` as a bounded child process with argv, never a shell.
A clean bound session uses its file; otherwise a private temporary KiCad copy is used,
with project and library-table companions and design rules copied alongside and library
paths resolved against the original project. The user's project is not written unless an
explicit output path requests it. Missing KiCad returns `kicad_unavailable` with an
install/`CHUDWARE_KICAD_CLI` hint; errors retain `{code,message}`.

Agent operations remain available; each accepted agent mutation is saved to its bound
KiCad file. The MCP `ecad_open`, `ecad_kicad_status`, and `ecad_find_footprint` tools
complement existing tools; import/export KiCad tools use open/save semantics. The
in-house netlist and incremental compile support simulation, routing and agent feedback;
KiCad ERC/DRC are authoritative.

The desktop Electronics workspace is a project hub with a read-only preview and
project files, status, checks, simulation, AI design, autorouting, board update,
fabrication and board-to-3D actions. With no document it presents New KiCad project,
Open project/file and AI design, plus install status and disabled unavailable actions.
It launches `kicad <project>`, `eeschema <schematic>` or `pcbnew <board>` as detached
argv processes, saving dirty agent work first. Schematic/PCB tabs use file names;
Save As selects `.kicad_sch` or `.kicad_pcb`. Polling `ecad_tree` refreshes a clean
preview after KiCad saves; dirty conflicts offer reload or keep the Chudware version.
Desktop launch is unavailable on web, while CLI work runs where hostd runs.

The retained Electronics command IDs are `ecad_ai_design`, `ecad_open`, `ecad_save`,
`ecad_run_erc`, `ecad_run_drc`, `pcb_update_from_sch`, `ecad_autoroute`,
`ecad_fill_zones`, `ecad_to_3d`, `ecad_simulate`, `ecad_export_bom`,
`sch_export_netlist`, and `sch_export_pdf`. The hub adds `ecad_new_project`,
`ecad_kicad_open_sch`, `ecad_kicad_open_pcb`, `ecad_kicad_open_project`,
`ecad_export_fab` (Gerber, drill, CPL and BOM under `<project>/fab/`) and
`ecad_export_step`. Canvas editing commands, old separate schematic/board creation,
and redundant KiCad/import/export and individual fabrication commands are removed.

MCP `ecad_new_schematic` and `ecad_new_pcb` accept optional `path` and `name`;
by default they create a project under `<workdir>/<name>/<name>.kicad_*`.
Accepted agent mutation batches save to the bound file and report `kicad_locked`
when KiCad holds its lock. `ecad_run_erc` and `ecad_run_drc` use the KiCad check;
`ecad_export`, `ecad_bom`, `ecad_plot`, `ecad_netlist(path=...)` and `ecad_to_3d`
use the CLI-backed operations.

### Recovery and external refresh (v2 clarification)

`ecad_save {doc_id, path?, recovery:true|metadata}` writes a KiCad snapshot under
`CHUDWARE_ECAD_AUTOSAVE_DIR`, or the platform APPDATA/temp `Chudware/ecad_autosave`
directory. With no path it generates a snapshot path. The adjacent
`<snapshot>.recovery.json` retains supplied pid, doc_id, name and path metadata,
human dirty state, revision, snapshot and baseline metadata, and the snapshot content
fingerprint. Opening the snapshot restores the original KiCad binding, name and dirty
state without overwriting the original file.

`ecad_tree` also returns `dirty`. When it reloads a changed clean file, it increments
the revision and returns `file_changed:true` on the triggering read.

`ecad_open`, `ecad_new`, `ecad_reload` and `ecad_tree` additionally return `project`,
the existing sibling `.kicad_pro` path, or an empty string when none exists.

Recovery metadata may specify `recovery_dir` to pass the services worker's actual
recovery root to hostd. An explicit snapshot path must be inside that root; when
the override is omitted, hostd uses its configured default recovery root.

PCB sessions persist design rules, net classes and assignments in native
`.kicad_pro` JSON; non-KiCad analysis fields use the namespaced KiCad text variable
`CHUDWARE_ECAD_ANALYSIS_V1`. PCB `ecad_tree` also watches the companion project's
fingerprint. Saving over a changed original project refuses `external_change`;
Save As to a different target is permitted. PCB recovery metadata also retains
`board_settings`, `saved_board_settings` and `project_baseline`.

`ecad_symbols` and `ecad_footprints` accept `resolve:true`, an exact ID in `query`,
and optional `doc_id` for project library context; a matching hit includes its
definition. Inline `ecad_update_from_sch` accepts `board_doc_id` for project library
context. Recovery metadata's optional `recovery_dir` is described above.
