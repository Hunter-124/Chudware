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

## 2. Simulation methods (sim worker, `sim_*` — isolated process)

| method | params → result |
|---|---|
| `sim_run` | `{doc_id|spice_path|circuit, study}` → `{ok, results, log, ms}` or streamed progress events |
| `sim_cancel` | `{run_id}` → `{cancelled}` |

`study` schema and `results` shape are in `sim.md`. `sim_run` emits `{event:"sim_progress", data:{phase, frac, msg}}` for long transients (same event envelope as FEA/CFD). A crash in the sim worker (`kill -9`) must leave the doc worker healthy — verified by test, same as gmsh.

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

### 5.2 Still declared, still unserved

`sim_run` / `sim_cancel {run_id}` (§2) describe an isolated circuit-sim worker. The shipped path is
the synchronous `ecad_simulate` plus the mechanical `sim_cancel {job_id}`. This divergence is
recorded here rather than papered over; closing it is a separate work item.
