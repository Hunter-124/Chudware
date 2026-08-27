# Contract: Netlist + ERC — v1

> The **single source of connectivity truth**. Derived from a `SchematicDocument`; consumed by ERC,
> PCB net-binding, simulation, and SPICE export. Pure function of the schematic (no side effects).

## 1. Netlist data model (output of `build_netlist(sch) -> Netlist`)

```json
{
  "nets": {
    "GND":  [{"sym":"s1","pin":"2","ref":"R1","type":"passive","xy":[x,y]}, ...],
    "VCC":  [...],
    "N$3":  [...]                            // auto-named net (no label) -> "N$<n>"
  },
  "by_pin": {"s1.2": "GND", "s2.1": "N$3", ...},   // every placed pin -> its net (or "" if floating)
  "components": [
    {"ref":"R1","libid":"Device:R","value":"10k","footprint":"...","sym":"s1",
     "pins":{"1":"N$3","2":"GND"},"fields":{...},"dnp":false}
  ],
  "warnings": [...]                          // non-fatal build notes
}
```

Net naming priority (highest wins): explicit **power port** net > **global label** > **local /
hier label** (per sheet) / connected **sheet-pin** name > auto `N$<n>` (stable per build: assigned
in deterministic geometric order so diffs are minimal). A net touched by a power port keeps the
power name even if also labeled. A hierarchy-spanning net (a parent sheet pin bridged to a child
`hier` label) is named by that shared name (which the child hier label and parent sheet pin agree
on) unless a higher-priority power/global name also touches the class.

## 2. Connectivity algorithm (normative)

1. **Snap** all endpoints to a `1e-6` mm tolerance grid; build a union-find over points.
2. **Wires**: union each consecutive vertex pair of a wire's `pts`. Two distinct wires union only
   where (a) they share an endpoint, or (b) a `Junction` lies on both (T/4-way). Crossing without a
   junction does **not** connect (KiCad behavior).
3. **Pins**: compute each placed pin's world endpoint; union with any coincident wire vertex or
   another coincident pin endpoint (direct pin-to-pin abutment is legal).
4. **Labels**: union all wire/pin points coincident with a label's `pos`; then union across labels
   of equal resolved name+scope (local: same sheet; global: all sheets).
5. **Power ports**: union coincident points; then union all ports of equal `net` globally.
6. **Buses**: a bus label `D[0..7]` expands to members; a bus-entry connects a wire to a named
   member. (v1: parse + expand; full bus routing is a documented follow-up.)
7. **Hierarchy** (sheet symbols): each sheet pin's `pos` attaches to the parent sheet's wires/pins
   like a label (on-segment OR coincident). Then, for every sheet symbol on a parent sheet
   instantiating child `C`, each sheet pin `"X"` is unioned with the `hier`-label `"X"` anchor in
   sheet `C` (equal name + same child). This is the cross-sheet bridge: the parent net touching the
   sheet pin and the child net touching the hier label become one class. Sheet pins and their child
   labels are iterated in sorted (id, name) order for determinism. Absent `sheet_symbols` (or no
   `hier` labels) this step is a no-op and the netlist is byte-identical to the flat behavior.
   `global` labels and power still merge across all sheets; `local` labels stay per-sheet.
8. **Name** each union-find class by §1 priority; emit `nets`, `by_pin`, `components`.

Determinism: every set/dict iteration is sorted by id/coordinate so the netlist is byte-stable for a
given schematic (golden-test requirement).

## 3. ERC rule catalog (`run_erc(sch, netlist) -> [Violation]`)

`Violation = {"rule":str,"severity":"error|warning","net":str|null,"pins":[pinref],"xy":[x,y],"message":str}`

| rule | severity | trigger |
|---|---|---|
| `pin_not_connected` | warning | a pin on a net of size 1 and not marked `no_connect` |
| `no_connect_connected` | error | a pin with a `NoConnect` flag is on a net of size >1 |
| `conflicting_drivers` | error | ≥2 `output`/`power_out` pins on one net (output-output short) |
| `no_driver` | warning | net has only `input` pins (no output/passive/power source) |
| `power_not_driven` | error | `power_in` pin on a net with no `power_out`/power port |
| `multiple_nets_label` | error | a label coincident with two different union classes |
| `unannotated_ref` | warning | symbol `ref` ends in `?` |
| `duplicate_ref` | error | two symbols share a non-`?` ref |
| `missing_footprint` | warning | `on_board` symbol has empty `footprint` |
| `floating_net_single_pin` | warning | net of exactly one pin (likely a wiring slip) |
| `hier_pin_no_label` | warning | a sheet pin with no matching `hier` label in the instantiated child sheet |
| `hier_label_no_pin` | warning | a `hier` label in an instantiated child sheet not exposed as a sheet pin |

The pin-type compatibility matrix (output↔output = error, etc.) lives as a table in `erc.py`;
contract: it is symmetric and `unspecified`/`free` never raise. ERC is **advisory** — it never
blocks ops; it surfaces in the RPC response and to the AI as a feedback block.

## 4. SPICE / sim handoff

`netlist_to_spice_circuit(netlist, sim_params) -> Circuit` (see `sim.md`) maps each component with a
`sim.model` to a device, using `sim.pin_order` to order nodes. Net `"GND"` (or any port named
`GND`/`0`) is forced to **node 0** (ground). Components without a sim model (connectors, mounting
holes) are skipped with a warning. This is the only path schematic→simulation.

## 5. v1.1 corrections (behavior fixes — the v1 text above described the intent, not the code)

### 5.1 Connectivity

1. **The union-find key is `(sheet, snapped point)`, not a bare point.** v1 keyed on `Point{x,y}`
   alone and inserted every wire vertex and pin into one global point space, so two sheets with
   coincident geometry were electrically shorted. Only global labels, power ports and matched
   sheet-pin↔hier-label pairs cross a sheet boundary.
2. **Endpoints attach to segment interiors.** A pin tip, or a wire endpoint, landing on another
   wire's interior is connected (KiCad's implicit T). A crossing where neither wire has an
   endpoint still requires an explicit `Junction`. v1 unioned only coincident vertices, so a pin
   touching the middle of a wire silently floated.
3. **Buses carry connectivity.** §2.6's "parse + expand only" is closed: `buses[]` polylines,
   `bus_entries[]` and bus labels now resolve member nets. `net_info[member].bus` names the owner.

### 5.2 Naming

Every connectivity class gets a stable internal id (`net_info[name].id`). The DISPLAY name still
follows §1's priority, but two DISTINCT classes may no longer collapse because their names
collide: the lower-priority one is disambiguated as `<name>@<sheet>`. v1 keyed the emitted `nets`
map on the bare label text and warned that distinct roots were "shorted-by-name" — that warning is
gone because the condition cannot occur.

### 5.3 Output additions

```json
{ "nets": {"GND": [{"sym","pin","ref","type","xy","sheet"}, …]},
  "by_pin": {…},
  "by_object": {"w3":"GND","l2":"GND","p1":"GND","b1":"D[0..7]"},
  "components": [{…,"syms":["s1","s3"],"units":2,"in_bom":true,"on_board":true,"datasheet":""}],
  "net_info": {"GND": {"id":"n4","sheets":["sh1"],"labels":[…],"bus":null,"members":6,
                       "drivers":["power_out"],"has_power_source":true}},
  "buses": {"D[0..7]": ["D0","D1", …]},
  "warnings": [ … ] }
```
`by_object` maps a document entity id to its net — the GUI's only way to highlight or dim a net
without re-deriving connectivity. `components` groups **multi-unit** instances: one entry per
physical reference with merged pins (v1 emitted one component per symbol instance, so two units of
U1 became two BOM lines and two footprints). `in_bom`/`on_board` now reach `sync`, which
previously dropped every DNP part and then reported its footprint as removable.

## 6. v1.1 ERC corrections and additions

**Drive levels** replace the v1 boolean: `power_out`=4, `output`=3,
`bidirectional`/`tri_state`/`open_collector`/`open_emitter`=2, `passive`/`free`/`unspecified`=1,
`input`/`power_in`=0. v1 classified `passive` as a driver, so an input-plus-resistor net never
raised `no_driver`.

**Power sources are real sources.** `has_power_source` is true only for a `power_out` pin or a
`PWR_FLAG` port. v1 treated *any* power port as a source, so a plain `GND` symbol silently
satisfied `power_not_driven`. The new `power_flag_needed` warning names the remedy.

**`no_connect_connected`** fires on ANY conductive attachment to the flagged pin. v1 required the
net to have >1 pin, so an NC pin wired to a dangling stub was missed. `no_connect_dangling` covers
a marker that touches no pin.

**`duplicate_ref`** permits two symbols sharing a ref when their `libid` matches and their `unit`
differs; a genuine collision is `unit_ref_conflict`.

Added rules: `conflicting_labels`, `similar_labels`, `missing_value`, `bus_member_mismatch`,
`sheet_pin_not_connected`, `unit_ref_conflict`, `symbol_off_grid`, `power_flag_needed`,
`pin_type_unspecified`, and `not_a_schematic` (so an empty result on a non-schematic cannot be
read as "clean"). The pin-type matrix is the full symmetric KiCad table over all twelve pin types,
not v1's four pairs. Every violation carries an `xy` when one is derivable — the canvas draws a
marker there.
