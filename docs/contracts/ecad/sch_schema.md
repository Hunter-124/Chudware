# Contract: Schematic Document Schema — v1

> `SchematicDocument` (`schema: "aicad.sch/1"`). JSON-serializable, the **authority for
> connectivity**. Mirrors the conventions of `docs/contracts/doc_schema.md`: canonical units are
> **mm internally** (display/grid in C++ only), ids are assigned and never reused, unknown keys are
> preserved on resave. Changes require a version bump.

## 1. Top-level

```json
{
  "schema": "aicad.sch/1",
  "id": "uuid4",
  "name": "Astable 555",
  "units": {"grid": 2.54},                 // mm; default 2.54 (100 mil)
  "sheets": [Sheet, ...],                   // at least one ("root")
  "symbols": [Symbol, ...],                 // placed component instances (flat list; each has sheet)
  "wires": [Wire, ...],
  "junctions": [Junction, ...],
  "labels": [Label, ...],
  "buses": [Bus, ...],
  "power": [PowerPort, ...],
  "no_connect": [NoConnect, ...],
  "graphics": [Graphic, ...],               // notes/lines/rects/text (non-electrical)
  "lib_cache": {libid: SymbolDef, ...},     // embedded copies so the doc is self-contained
  "annotations": {"next_ref": {"R": 3}}     // ref-des auto-number bookkeeping (advisory)
}
```

## 2. Coordinates & ids

- All positions are `[x, y]` in **mm**, sheet-local. +x right, +y **up** (math convention; the C++
  canvas flips for screen). Angles in **degrees CCW**.
- Id prefixes (never reused, per-document counters): symbols `s1`, pins are addressed
  `"s1.3"` (symbol id + pin number/name), wires `w1`, junctions `j1`, labels `l1`, buses `bus1`,
  power ports `pw1`, no-connects `nc1`, sheets `sh1`, graphics `g1`, sheet symbols `hs1`.

## 3. SymbolDef (library definition; in `lib_cache` and library files)

```json
{
  "libid": "Device:R",                      // "<lib>:<name>"
  "name": "R", "description": "Resistor",
  "keywords": "res resistor",
  "reference_prefix": "R",                  // default ref-des prefix
  "value": "R",                             // default value text
  "footprint_filters": ["R_*"],
  "pins": [Pin, ...],
  "graphics": [Graphic, ...],               // body drawing in symbol-local mm
  "units_count": 1,                         // multi-unit parts (e.g. quad op-amp = 4)
  "power": false,                           // true for power symbols (VCC/GND)
  "sim": {"model": "R", "pin_order": ["1","2"]}  // optional: maps to engine/sim device (see sim.md)
}
```

### 3.1 Pin

```json
{
  "number": "3",                            // pin number (string; "3", "A1", ...)
  "name": "OUT",
  "pos": [x, y], "angle": 0,                // root at body; angle points AWAY from body
  "length": 2.54,
  "type": "input|output|bidirectional|tri_state|passive|power_in|power_out|open_collector|open_emitter|unspecified|no_connect|free",
  "shape": "line|inverted|clock|...",       // cosmetic
  "unit": 1                                 // which sub-unit (multi-unit parts)
}
```

Pin `type` drives ERC (see `netlist.md`). `passive` for R/L/C; `power_in` for IC supply pins;
`power_out` for regulator outputs and power symbols.

## 4. Symbol (placed instance)

```json
{
  "id": "s1",
  "libid": "Device:R",
  "sheet": "sh1",
  "ref": "R1",                              // ref-des; "R?" if unannotated
  "value": "10k",
  "footprint": "Resistor_SMD:R_0603_1608Metric",  // footprint assoc (links to pcb)
  "pos": [x, y], "angle": 0, "mirror": "none|x|y",
  "unit": 1,
  "fields": {"MPN": "...", "Tolerance": "1%"},      // arbitrary user fields (BOM-visible)
  "sim_params": {"value": "10k"},           // optional override of SymbolDef.sim params
  "in_bom": true, "on_board": true, "dnp": false    // do-not-populate flag
}
```

Pin world position = symbol `pos` + rotate/mirror(SymbolDef pin `pos`). The compiler precomputes a
`pin_world` map; tools never hand-roll the transform (helper provided in `schema.py`).

## 5. Wire / Junction / Label / Bus / Power / NoConnect

```json
Wire:      {"id":"w1","sheet":"sh1","pts":[[x,y],[x,y],...],"bus":false}   // orthogonal polyline
Junction:  {"id":"j1","sheet":"sh1","pos":[x,y]}                           // explicit dot (4-way+ connect)
Label:     {"id":"l1","sheet":"sh1","pos":[x,y],"text":"CLK","scope":"local|global|hier","angle":0}
Bus:       {"id":"bus1","sheet":"sh1","pts":[...],"members":["D0","D1",...]}  // bus expansion
PowerPort: {"id":"pw1","sheet":"sh1","pos":[x,y],"net":"GND","symbol":"GND|VCC|+5V|..."}
NoConnect: {"id":"nc1","sheet":"sh1","pos":[x,y]}                          // marks a pin intentionally open
```

Connectivity (full rules in `netlist.md`): two endpoints connect when **coincident** (within
`1e-6` mm after grid snap). A wire crossing another wire's *midpoint* connects **only** if a
`Junction` sits there. A pin connects to a wire when the pin's world endpoint is coincident with a
wire vertex/endpoint. Labels of equal `text` (respecting scope/sheet) merge nets. Power ports of
equal `net` merge globally.

## 6. Sheet (hierarchy)

```json
{"id":"sh1","name":"root","parent":null,"size":[297,210],
 "instances_of":null,                       // non-null => this sheet embeds another sheet file/def (reserved)
 "ports":[{"name":"CLK","type":"input","pos":[x,y]}]}  // reserved per-sheet port list
```

A document carries a flat `sheets` list (at least one root). Hierarchy is layered on top via a
separate top-level **`sheet_symbols`** list (NOT by overloading the sheet object — keeping it a
distinct list mirrors how `symbols`/`wires`/etc. are flat, sheet-tagged lists, and keeps sheet-object
round-tripping unchanged):

```json
SheetSymbol: {
  "id":"hs1",                               // id prefix "hs" (never reused)
  "sheet":"sh1",                            // PARENT sheet this symbol is drawn on
  "child":"sh2",                            // the child sheet it instantiates
  "pos":[x,y],
  "pins":[{"name":"CLK","pos":[x,y],"shape":"input|output|bidirectional|tri_state|passive"}]
}
```

A sheet symbol is the parent-side instantiation of a child sheet; its **sheet pins** are named
connection points placed in the parent (like a component's pins). The child sheet exposes matching
**hierarchical labels** (`Label` with `scope:"hier"`) of the same name. Connectivity bridges the
two: a parent net touching sheet pin `"CLK"` of a symbol instantiating child `C` is the SAME net as
the `hier` label `"CLK"` in sheet `C` (see `netlist.md` §2). `global` labels and power still merge
across all sheets; `local` labels stay per-sheet.

Additive + backward-compatible: a document with no `sheet_symbols` (or none with `scope:"hier"`
labels) validates and netlists byte-identically to the pre-hierarchy flat behavior. `instances_of`
and the per-sheet `ports` list remain reserved.

## 7. Validation (`schema.py`)

- `validate_schematic(doc) -> []|[errors]`. Checks: schema tag, id uniqueness & prefix, every
  `symbol.libid` resolves in `lib_cache`, pin `unit` in range, wire `pts` length ≥2 and orthogonal
  (warn-only if diagonal), label scope enum, power `net` non-empty, sheet parent acyclic. Sheet
  symbols (`sheet_symbols`, prefix `hs`): `child` is a known sheet id, `pos` is `[x,y]`, each pin
  has a non-empty unique `name`, a `[x,y]` `pos`, and a valid `shape`. Absent `sheet_symbols`
  validates exactly as before.
- Numeric values stored as plain floats (mm). No expressions in v1 (CAD's equation engine is not
  wired into ECAD yet — reserved).
- Forward-compat: unknown top-level/object keys preserved verbatim on load→save round-trip.
