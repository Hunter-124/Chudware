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
