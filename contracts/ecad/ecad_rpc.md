# Contract: ECAD Worker RPC — v1

> ECAD methods on the existing JSON-RPC worker (`engine/worker.py`, protocol 2, newline-delimited
> JSON over stdio). Registered by appending `engine/ecad/rpc.py`'s `METHODS` dict into the worker's
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

`mcp_server/ecad_tools.py` calls the kernel/engine **directly** (not via the worker socket) — same
pattern as the existing `mcp_server/server.py` `run_model`. Worker RPC is for the C++ host; MCP is
for external/agent callers. Both share the same `engine/ecad` core, so behavior is identical.
