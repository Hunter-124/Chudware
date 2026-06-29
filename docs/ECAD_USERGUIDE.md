# AI-ECAD User Guide

> Practical reference for the AI-ECAD electronics design engine.
> For build status and remaining work see `docs/ECAD_STATUS.md`.
> For the full plan see `docs/ECAD_PLAN.md`.

---

## Overview

AI-ECAD is a headless electronics-design engine that lives in `engine/ecad/` and
`engine/sim/`. It covers the full PCB design pipeline from natural-language
prompt to fabrication-ready outputs:

```
AI prompt  ->  Schematic  ->  Netlist  ->  ERC  ->  Simulation
                     |
                    PCB  ->  Autoroute  ->  Zone Fill  ->  DRC
                                 |
                    Gerber / Drill / BOM / CPL  ->  3D STEP
```

The engine is exposed to the host application over a worker RPC
(`engine/ecad/rpc.py`, 16 `ecad_*` methods) and to AI agents through 18 MCP
tools (`mcp_server/ecad_tools.py`). It mirrors the architecture of the
existing `engine/doc` CAD pipeline (schema / ops / session / compile / RPC).

---

## Document Model

### Two document kinds

| Kind | Schema string | Factory | Description |
|---|---|---|---|
| Schematic | `aicad.sch/1` | `schema.new_schematic(name)` | Symbols, wires, labels, power ports, buses |
| Board | `aicad.pcb/1` | `schema.new_board(name)` | Footprints, tracks, vias, zones, outline |

Both validate with `schema.validate_schematic(doc)` / `schema.validate_board(doc)`.

### Schematic fields

```
{
  "schema": "aicad.sch/1",
  "id":     str,
  "name":   str,
  "units":  {...},
  "symbols":    [SymbolInstance, ...],   // placed components
  "wires":      [Wire, ...],
  "labels":     [Label, ...],            // local / global / hier
  "junctions":  [Junction, ...],
  "no_connect": [NoConnect, ...],
  "power":      [PowerPort, ...],
  "buses":      [Bus, ...],
  "lib_cache":  {libid: SymbolDef}       // local copy of lib symbols
}
```

### Board fields

```
{
  "schema": "aicad.pcb/1",
  "id":     str,
  "name":   str,
  "stackup":    {"copper_layers": int, "thickness": float, ...},
  "nets":       [{"name": str, "class": str}, ...],
  "footprints": [Footprint, ...],
  "tracks":     [Track, ...],
  "vias":       [Via, ...],
  "zones":      [Zone, ...],
  "outline":    {"shapes": [...], "closed": bool}
}
```

---

## Op Vocabulary

Ops are applied with `engine.ecad.ops.apply_ops(doc, ops_list)` or via a
live session. Each op is a dict with an `"op"` key.

### Schematic ops

| Op | Key args | What it does |
|---|---|---|
| `sch_add_symbol` | `libid`, `pos`, `ref`, `value` | Place a component from the library |
| `sch_move_symbol` | `id`, `pos` | Move a placed symbol |
| `sch_rotate_symbol` | `id`, `angle` | Rotate (degrees, CCW) |
| `sch_mirror_symbol` | `id`, `axis` | Mirror across x or y |
| `sch_delete_symbol` | `id` | Delete a placed symbol |
| `sch_set_property` | `id`, `key`, `value` | Set ref/value/footprint/field |
| `sch_add_wire` | `pts` | Add a wire segment list |
| `sch_delete_wire` | `id` | Delete a wire |
| `sch_add_label` | `text`, `pos`, `scope` | Add a net label (local/global/hier) |
| `sch_delete_label` | `id` | Delete a label |
| `sch_add_power` | `net`, `pos` | Add a power port (driver symbol) |
| `sch_add_junction` | `pos` | Add a T-junction dot |
| `sch_add_no_connect` | `pos` | Mark an intentionally unconnected pin |
| `sch_add_bus` | `pts` | Add a bus segment |

### PCB ops

| Op | Key args | What it does |
|---|---|---|
| `pcb_add_footprint` | `fpid`, `ref`, `pos`, `side`, `pads`, `def` | Place a footprint |
| `pcb_move_footprint` | `id`, `pos` | Move a footprint |
| `pcb_rotate_footprint` | `id`, `angle` | Rotate a footprint |
| `pcb_flip_footprint` | `id` | Flip top/bottom |
| `pcb_delete_footprint` | `id` | Remove a footprint |
| `pcb_set_outline` | `shapes`, `closed` | Set the Edge.Cuts board outline |
| `pcb_add_track` | `net`, `layer`, `p1`, `p2`, `width` | Add a copper track segment |
| `pcb_delete_track` | `id` | Delete a track |
| `pcb_add_via` | `net`, `at`, `drill`, `diameter`, `layers` | Add a via |
| `pcb_delete_via` | `id` | Delete a via |
| `pcb_add_zone` | `net`, `layer`, `polygon` | Add a copper pour zone |
| `pcb_delete_zone` | `id` | Delete a zone |
| `pcb_set_net` | `name`, `class` | Register / update a net |

Full contracts: `contracts/ecad/ecad_ops.md`.

---

## RPC Methods (worker boundary)

The 16 `ecad_*` RPC methods in `engine/ecad/rpc.py` are the boundary between
the Python engine and the C++ host. Each call returns an ops-response envelope:

```
{
  "revision": int,
  "id_map":   {client_id: server_id},
  "kind":     "sch" | "pcb",
  "tree":     <full document dict>,
  "compile":  {
      "netlist":  <netlist or null>,
      "erc":      [violation, ...],
      "drc":      [violation, ...],
      "ratsnest": [airwire, ...],
      "stats":    {...}
  }
}
```

| RPC method | What it does |
|---|---|
| `ecad_new` | Create a new sch/pcb session |
| `ecad_open` | Open a .ecad+ / .kicad_sch / .kicad_pcb file |
| `ecad_save` | Write the current document to a .ecad+ file |
| `ecad_close` | Close a session |
| `ecad_apply_ops` | Apply an op batch (compile, return response) |
| `ecad_undo` / `ecad_redo` | Undo/redo (bounded 100-deep JSON-snapshot stacks) |
| `ecad_rebuild` | Force recompile (netlist/ERC or DRC/ratsnest) |
| `ecad_tree` | Return the current tree + compile (no rebuild) |
| `ecad_simulate` | Netlist -> SPICE -> simulate a study |
| `ecad_autoroute` | Run the 2-layer Lee/maze grid router |
| `ecad_fill_zones` | Recompute all copper zone fills (full resolution) |
| `ecad_update_from_sch` | Back-annotate board from a schematic (sync) |
| `ecad_run_design` | AI orchestrator: NL request -> verified schematic |
| `ecad_describe` | Return a compact, human-readable board summary |
| `ecad_list_sessions` | List all open ECAD sessions |
| `ecad_status` | Detailed status of one session (ERC/DRC/ratsnest) |

---

## AI Tools (MCP surface)

`mcp_server/ecad_tools.py` exposes 18 tools for the AI agent layer.
Do **not** import the MCP server directly; use the underlying engine modules.

| Tool | What it does |
|---|---|
| `ecad_new_schematic` | Create a new schematic session |
| `ecad_new_board` | Create a new board session |
| `ecad_open` | Open a file |
| `ecad_save` | Save a session |
| `ecad_apply_ops` | Apply a JSON op list |
| `ecad_undo` / `ecad_redo` | Undo/redo |
| `ecad_status` | Current ERC/DRC/ratsnest + component list |
| `ecad_run_design` | Natural-language -> verified schematic via orchestrator |
| `ecad_simulate` | Run a simulation study on the current schematic |
| `ecad_step_sweep` | Run a .step parameter sweep |
| `ecad_monte_carlo` | Run a Monte-Carlo tolerance analysis |
| `ecad_autoroute` | Route the unconnected ratsnest |
| `ecad_fill_zones` | Fill all copper pours |
| `ecad_export_gerbers` | Write Gerber + drill + BOM to a directory |
| `ecad_export_step` | Write the board as a 3D STEP file |
| `ecad_update_from_sch` | Sync a board from a schematic |
| `ecad_import_kicad` | Import a .kicad_sch / .kicad_pcb file |
| `ecad_describe` | Natural-language board summary |

---

## Simulation Analyses

The simulator (`engine/sim/circuit.py`) is a native Modified Nodal Analysis
(MNA) engine. Entry point: `simulate(circuit, study) -> dict`.

The `circuit` dict comes from `netlist_to_spice_circuit(netlist)` or from
parsing a SPICE deck with `parse_spice_deck(text)`.

### Study shape

```python
study = {
    "analyses": [<analysis_dict>, ...],  # one or more
    "probes":   ["V(NODE)", "I(V1)", ...],  # empty -> all nodes
    "options":  {"gmin": 1e-12, "reltol": 1e-3, ...},
    "seed":     12345,  # for Monte-Carlo
}
```

### Analysis types

| Type | Dict shape | Returns |
|---|---|---|
| `op` | `{"type": "op"}` | `result["op"]`: probe -> value |
| `dc` | `{"type": "dc", "source": "V1", "start": 0, "stop": 5, "step": 0.1}` | `result["dc"]["traces"]` |
| `tran` | `{"type": "tran", "tstep": 1e-5, "tstop": 1e-3, "uic": True}` | `result["tran"]["x_t"]`, `"traces"` |
| `ac` | `{"type": "ac", "sweep": "dec", "points": 20, "fstart": 1, "fstop": 1e6}` | `result["ac"]["x_f"]`, `"mag"`, `"phase_deg"` |
| `step` | `{"type": "step", "param": "R1", "values": [500, 1000, 2000]}` + inner analyses | `result["step"]["runs"]` |
| `mc` | `{"type": "mc", "runs": 100, "probe": "V(OUT)", "tol": {"R": 0.01}, "analysis": {...}}` | `result["mc"]`: mean/std/min/max |
| `four` | `{"type": "four", "f0": 1000, "probe": "V(OUT)"}` + prior tran | `result["four"]`: harmonics, thd_pct |
| `temp` | `{"type": "temp", "values": [-40, 25, 85], "analysis": {...}}` | `result["temp"]["points"]` |
| `noise` | `{"type": "noise", "output": "V(OUT)", "fstart": 1, "fstop": 1e6}` | `result["noise"]` |

### Probe syntax

- `V(NODE)` -- node voltage referenced to ground
- `V(A,B)` -- differential voltage A-B
- `I(V1)` -- branch current through source V1
- `I(R1)` -- current through a resistor (derived from node voltages)

### Convergence

`result["convergence"]` is a dict of `{analysis_type: bool}`. A False entry
means the solver did not converge (the result may contain NaN values); the
simulation never raises on a non-convergent circuit.

---

## Fabrication Outputs

`engine/ecad/fab/` provides four writers, re-exported from `engine/ecad/fab/__init__.py`:

```python
from engine.ecad.fab import write_gerbers, write_drill, write_bom, write_placement
```

| Function | Returns | Notes |
|---|---|---|
| `write_gerbers(board, out_dir)` | `{layer: file_path}` | RS-274X Gerber, one file per layer |
| `write_drill(board, out_dir)` | `{kind: file_path}` | Excellon drill (PTH + NPTH) |
| `write_bom(board, fmt="csv")` | `str` | CSV or JSON BOM |
| `write_placement(board)` | `str` | Pick-and-place CSV (centroid, rotation, side) |

---

## 3D Bridge

`engine/ecad/threed.py` converts a board to a STEP solid and a CAD document
that can be opened in the AI-CAD 3D viewport.

```python
from engine.ecad.threed import board_to_step, board_to_part_doc

# Board substrate + (optionally) component proxies -> STEP
result = board_to_step(board, "/tmp/board.step",
                       opts={"components": True, "copper": False})
# result["step_path"]  -- absolute path
# result["summary"]    -- board_volume, outline_area, thickness, n_holes, ...

# Minimal aicad.part/1 doc referencing the STEP (opens in CAD viewport)
part_doc = board_to_part_doc(board, result["step_path"])
```

`opts` keys:
- `components` (bool, default True) -- place component models / box proxies
- `copper` (bool, default False) -- extrude track copper (heavy)
- `z_origin` (`"bottom"` or `"center"`)
- `mask_color`, `copper_color` -- RGB tuples

Requires build123d / OCP. The demo guards this in `try/except` so it always
prints a clean SKIP line when OCP is unavailable.

---

## File Formats and KiCad Interop

`engine/ecad/formats.py` public API:

```python
from engine.ecad import formats

# Save to .ecad+ ZIP container
path = formats.save(doc, "/path/to/design.ecad+")

# Open .ecad+ or .kicad_sch or .kicad_pcb
doc, warnings = formats.open("/path/to/file.ecad+")

# Import a KiCad schematic (path or raw S-expression text)
sch_doc = formats.import_kicad_sch("/path/to/design.kicad_sch")
pcb_doc = formats.import_kicad_pcb("/path/to/design.kicad_pcb")
```

### .ecad+ container

A ZIP file containing:
- `document.json` -- the full ECAD document
- `meta.json` -- schema/version/timestamps
- `netlist.json` -- last built netlist snapshot (optional)
- `thumbnail.png` -- placeholder or rendered thumbnail

Unknown extra entries are preserved verbatim on resave (forward-compatible).

### KiCad interop

The importers parse KiCad 6/7/8 S-expression format. They produce
`aicad.sch/1` and `aicad.pcb/1` documents that pass the native validators.
No KiCad source code is used (clean-room, GPL-clean).

---

## Sch->PCB Sync (Back-Annotation)

`engine/ecad/sync.update_board_from_sch` reconciles a board against a
schematic netlist, mirroring KiCad "Update PCB from Schematic":

```python
from engine.ecad.sync import update_board_from_sch
from engine.ecad.netlist import build_netlist

nl = build_netlist(schematic_doc)
result = update_board_from_sch(board_doc, nl)
new_board = result["mutated_board"]
report    = result["report"]
# report keys: added, updated, removed, unresolved, nets_bound, linked_sch
```

The function:
1. For each placeable component (on-board, not DNP, not a power symbol, has a
   footprint fpid), resolves the footprint via the starter library.
2. Adds a footprint if none exists for that ref.
3. Refreshes ref/value and binds pad nets from the netlist.
4. Reports removed footprints (optionally prunes them with `prune=True`).
5. Is idempotent: running it twice yields an empty added/updated report.

---

## AI Orchestrator

`engine/ecad/orchestrator.run_design(request, provider=None)` runs a
deterministic state machine that turns a natural-language request into a
verified schematic:

```
PLAN  ->  BUILD  ->  ERC (+FIX)  ->  SIMULATE  ->  REPORT
```

- `provider=None` skips the LLM call and uses the closest **recipe fallback**
  so the run always produces a result without a model or network.
- Returns a `DesignRun` dataclass with `ok`, `erc`, `sim`, `stages`,
  `ops_applied`, `stop_reason`, and `recipe_used`.
- `stop_reason` mirrors the agent loop: `confirmed`, `fallback`, `failed`,
  `max_attempts`.

### Recipes

`engine/ecad/recipes.py` provides four parameterized known-good designs:

| Function | Design |
|---|---|
| `voltage_divider(vin, vout)` | Resistive divider, R1+R2=20k |
| `rc_lowpass(fc)` | R=1k, C=1/(2*pi*fc*R), AC study |
| `inverting_amp(gain)` | Op-amp with Rin=1k, Rf=gain*Rin |
| `rc_555_astable(freq)` | 555 astable Ra=Rb=R, C=1uF, transient study |

`recipes.closest_recipe(request)` keyword-scores the request and parameterizes
the winner from any numeric in the text ("a 10kHz lowpass" -> `rc_lowpass(fc=10000)`).

---

## 5-Minute Quickstart

```python
import copy, tempfile, os
from engine.ecad import schema, formats
from engine.ecad.ops import apply_ops
from engine.ecad.library.starter import SYMBOLS, FOOTPRINTS
from engine.ecad.netlist import build_netlist
from engine.ecad.erc import run_erc
from engine.sim.spice import netlist_to_spice_circuit
from engine.sim.circuit import simulate
from engine.ecad.drc import ratsnest
from engine.ecad import autoroute as ar_mod, zonefill as zf_mod
from engine.ecad.fab import write_gerbers, write_drill, write_bom

# --- 1. Build a 5V voltage divider schematic ---
doc = schema.new_schematic("quickstart")
for libid in ("Simulation_SPICE:V_source", "Device:R"):
    doc["lib_cache"][libid] = copy.deepcopy(SYMBOLS[libid])

res = apply_ops(doc, [
    {"op": "sch_add_symbol", "libid": "Simulation_SPICE:V_source",
     "pos": [0.0, 0.0], "ref": "V1", "value": "5"},
    {"op": "sch_add_symbol", "libid": "Device:R",
     "pos": [20.0, 5.0], "ref": "R1", "value": "10k"},
    {"op": "sch_add_symbol", "libid": "Device:R",
     "pos": [40.0, 5.0], "ref": "R2", "value": "10k"},
])
doc = res["doc"]
apply_ops(doc, [
    {"op": "sch_add_power", "net": "+5V", "pos": [0.0, 5.08]},
    {"op": "sch_add_power", "net": "GND", "pos": [0.0, -5.08]},
    {"op": "sch_add_label", "text": "VMID", "pos": [25.08, 5.0], "scope": "local"},
    {"op": "sch_add_label", "text": "VMID", "pos": [34.92, 5.0], "scope": "local"},
])

# --- 2. Netlist + ERC ---
nl = build_netlist(doc)
errs = run_erc(doc, nl)
print(f"ERC: {sum(1 for v in errs if v['severity']=='error')} errors")

# --- 3. Simulate (DC operating point) ---
circuit, _ = netlist_to_spice_circuit(nl)
result = simulate(circuit, {"analyses": [{"type": "op"}], "probes": ["V(VMID)"]})
vmid = result["op"]["V(VMID)"]
print(f"V(VMID) = {vmid:.4g} V  (expected 2.5 V)")

# --- 4. Build a PCB board ---
board = schema.new_board("quickstart_pcb")
board["nets"] += [{"name": "+5V", "class": "default"},
                  {"name": "VMID", "class": "default"}]
apply_ops(board, [{"op": "pcb_set_outline",
    "shapes": [
        {"type": "line", "p1": [0,0],  "p2": [30,0]},
        {"type": "line", "p1": [30,0], "p2": [30,20]},
        {"type": "line", "p1": [30,20],"p2": [0,20]},
        {"type": "line", "p1": [0,20], "p2": [0,0]},
    ], "closed": True}])

fp0603 = FOOTPRINTS["Resistor_SMD:R_0603"]
for ref, pos, pad_nets in [
        ("R1", [8.0, 10.0], {1: "+5V", 2: "VMID"}),
        ("R2", [20.0, 10.0], {1: "VMID", 2: "GND"})]:
    pads = copy.deepcopy(fp0603["pads"])
    for pad in pads:
        pad["net"] = pad_nets.get(int(pad.get("number", 0)))
    apply_ops(board, [{"op": "pcb_add_footprint",
        "fpid": "Resistor_SMD:R_0603",
        "ref": ref, "value": "10k", "pos": pos, "side": "top",
        "pads": pads, "def": fp0603}])

# --- 5. Autoroute + zone fill ---
rn_before = ratsnest(board)
print(f"Ratsnest before: {len(rn_before)} airwires")
route_res = ar_mod.autoroute(board, {})
board["tracks"] += route_res["tracks"]
rn_after = ratsnest(board)
print(f"Ratsnest after:  {len(rn_after)} airwires")

apply_ops(board, [{"op": "pcb_add_zone", "net": "GND", "layer": "F.Cu",
                   "polygon": [[1,1],[29,1],[29,19],[1,19]]}])
filled = zf_mod.fill_zones(board)
board["zones"] = filled.get("zones", board.get("zones", []))

# --- 6. Export ---
with tempfile.TemporaryDirectory() as tmpdir:
    gerbers = write_gerbers(board, os.path.join(tmpdir, "gerbers"))
    print(f"Gerber files: {list(gerbers)}")
    bom = write_bom(board, fmt="csv")
    print("BOM:", bom.strip())
    ecad_path = formats.save(doc, os.path.join(tmpdir, "design.ecad+"))
    print(f"Saved: {ecad_path}")
```

---

## Architecture Notes

- **No KiCad source** -- the library parser reads `.kicad_sym` / `.kicad_mod`
  files that belong to the user; no GPL code is included.
- **Native MNA simulator** -- pure Python + numpy/scipy; no ngspice subprocess.
  Handles R/L/C/V/I/D/Q/M + OPAMP and NE555 behavioral subckts.
- **Deterministic** -- every netlist build, ERC run, ratsnest computation, and
  Monte-Carlo draw (with a seed) produces byte-identical output for the same
  input.
- **Compile-on-apply** -- every `apply_ops` call runs a best-effort
  netlist+ERC (schematic) or DRC+ratsnest (board) compile and includes the
  result in the ops-response, so the host always has fresh diagnostic data
  after any edit.
- **Mirrors engine/doc** -- schema / ops / session / compile / RPC layers are
  the same pattern as the 3D CAD pipeline, so both halves of the app share
  the same idioms and the same agent-loop discipline.
