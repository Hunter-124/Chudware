# aicad.boardphys/1 — board physics sidecar + component engineering metadata

Status: FROZEN v1 (append-only, like every contract in contracts/). New optional keys may be
added; existing keys never change meaning or type. A breaking change mints `aicad.boardphys/2`.

This contract has two halves:

1. **Library `eng` blocks** — optional engineering metadata carried on symbol defs and
   footprint defs in the component library (`engine/ecad/library/`), plus per-instance
   overrides.
2. **The `aicad.boardphys/1` sidecar** — the machine-readable map emitted by
   `engine.ecad.threed.board_to_step` (surfaced by the `ecad_to_3d` MCP tool as a
   `*.boardphys.json` file next to the STEP), mapping every placed component solid to its
   power/thermal metadata. This is THE seam consumed by host-board rule packs, PDN checks,
   thermal handoff, and power-budget tooling (T-FIN13/16/21/23).

## Honesty rule (binding on every producer)

Missing metadata degrades to **`null`, never an invented `0`**. A consumer must treat
`null` as "unknown — go measure/look it up", and `0` as a real engineering claim.
Special case: `supply.sequencing == []` means *known: no sequencing requirement*
(single-rail device); `null` means *unknown*.

Every seeded value carries a **source note** in `sources` (a `research_*`-style provenance
string naming the datasheet/standard). Values without provenance must not be added to the
bundled library.

## 1. Library `eng` blocks

### On a symbol def (electrical, device-class data)

```json
"eng": {
  "power_w":        {"typ": 0.015, "max": 0.225},
  "limits_c":       {"tj_max": 150.0, "ta_max": 70.0},
  "theta_c_per_w":  {"ja": null, "jc": null},
  "supply": {
    "rails": [{"name": "VCC", "voltage_v_min": 4.5, "voltage_v_nom": 5.0,
               "voltage_v_max": 16.0, "current_a_active": 0.003,
               "current_a_sleep": null}],
    "voltage_domains": ["VCC"],
    "sequencing": []
  },
  "sources": ["research_datasheet_fetch: TI NE555 datasheet (SLFS022) — ..."]
}
```

All keys optional; partial blocks are fine (the resolver fills the rest with `null`).

- `power_w.typ` / `power_w.max` — watts. For passives, `max` is the *rated* dissipation of
  the standard part in that package; `typ` (actual operating dissipation) stays `null`
  until a simulation or per-instance override provides it.
- `limits_c.tj_max` / `ta_max` — junction / ambient temperature limits, °C.
- `supply.rails[*]` — per-rail envelope: voltage min/nom/max (V) + supply current in
  active and sleep states (A).
- `supply.voltage_domains` — rail names this device belongs to.
- `supply.sequencing` — list of human-readable sequencing constraints
  (e.g. `"VCORE before VIO"`). `[]` = none required. `null` = unknown.

### On a footprint def (physical, package-class data)

```json
"eng": {
  "package": {"shape": "box", "l_mm": 4.9, "w_mm": 3.9, "h_mm": 1.75, "d_mm": null},
  "theta_c_per_w": {"ja": 120.0},
  "power_w": {"max": 0.125},
  "sources": ["research_standard_section: JEDEC/IPC-7351 nominal package outlines ..."]
}
```

- `package.shape` — `"box"` (needs `l_mm`/`w_mm`/`h_mm`) or `"cylinder"` (needs
  `d_mm`/`h_mm`). Dims are the nominal plastic **body**, not lead span. `ecad_to_3d` uses
  these to place a real component solid instead of the pad-extent proxy.
- `theta_c_per_w.ja` — JEDEC-style single-part-on-standard-board value; layout-dependent,
  seed only.

### Per-instance overrides

A schematic symbol instance may carry `sim_params.eng` (same partial shape). The netlist
copies it onto the component (`eng_overrides`), and `update_board_from_sch` carries it onto
the board footprint as `footprint.eng_overrides` (a direct `footprint.fields.eng` dict is
also honored). Board footprints also gain a `libid` back-link to the placing symbol.

### Merge precedence (resolver: `engine/ecad/metadata.py`)

```
footprint eng  <  symbol eng  <  per-instance override      (later wins)
```

Dicts deep-merge; scalars/lists replace; a `null` in an override is a no-op (an override
can never erase known data into unknown). `sources` **accumulate** across all three layers
so provenance is never silently lost. `resolve_eng()` / `eng_for_symbol()` /
`eng_for_footprint()` return the fully-normalized dict with every key present.

## 2. The sidecar document

```json
{
  "schema": "aicad.boardphys/1",
  "generated_by": "engine.ecad.threed.board_to_step",
  "board": {"name": "myboard", "id": "<uuid>", "thickness_mm": 1.6,
            "outline_area_mm2": 800.0},
  "step_path": "/abs/path/board.step",
  "components": [
    {
      "ref": "U1", "id": "fp3",
      "fpid": "Package_SO:SOIC-8", "libid": "Timer:NE555",
      "solid_label": "U1",
      "pos_mm": [10.0, 5.0], "angle_deg": 0.0, "side": "top",
      "body": {"shape": "box", "l_mm": 4.9, "w_mm": 3.9, "h_mm": 1.75,
               "d_mm": null, "source": "package_lib"},
      "power_w": {"typ": 0.015, "max": 0.225},
      "theta_c_per_w": {"ja": 120.0, "jc": null},
      "limits_c": {"tj_max": 150.0, "ta_max": 70.0},
      "supply": {"rails": [...], "voltage_domains": ["VCC"], "sequencing": []},
      "sources": ["research_datasheet_fetch: ..."]
    }
  ],
  "totals": {"power_w_typ": 0.035, "power_w_max": 0.35,
             "n_components": 3, "n_with_power": 2, "n_missing_power": 1}
}
```

Field semantics:

- One entry per `on_board` footprint (excluded/virtual footprints are omitted). A
  footprint that could not be placed keeps its entry with `solid_label: null` and
  `body: null` — consumers can still budget its power.
- `solid_label` matches the label of the component solid inside the exported STEP
  compound (the substrate is labelled `PCB_Substrate`).
- `body.source` — `"package_lib"` (library package dims), `"proxy_extent"` (pad-extent
  fallback box; dims describe the proxy, not the real part), or `"model_3d"` (a real STEP
  model was placed; dims are `null` — measure the solid).
- `pos_mm`/`angle_deg`/`side` — footprint pose in board coordinates (mm, degrees CCW; same
  convention as `aicad.pcb/1`).
- `totals` — board power rollup. Sums cover only components with data; when **no**
  component has power data the sums are `null` (honest unknown, per the honesty rule).
  `n_missing_power` tells consumers the coverage gap.

## Consumers (the reason this seam is frozen)

- Host-board rule packs — component placement/keep-out vs power class.
- PDN checks — `supply.rails` currents per voltage domain.
- Thermal handoff — `power_w` + `theta_c_per_w` + `limits_c` + body geometry → FEA thermal.
- Power budget / vibe checks — `totals` + per-component `power_w`.

MCP surfaces: `ecad_to_3d` writes the sidecar and returns its path + totals;
`ecad_status` and `describe_board` narrate per-component power and the board total.
