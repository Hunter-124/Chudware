# Contract: `aicad.harness/1` — v1

> **Frozen seam.** This section is append-only; changes to it require the versioning
> policy in [README.md](README.md). Executable model and persistence:
> `services/harness/model.py`. This is a document study, not a separate document kind.

## 1. Study and model

A document may contain multiple studies with `type: "harness"`, `name` equal to the
harness id and `engine: "py-harness"`. The study stores the model at
`params.harness`, with `params.schema: "aicad.harness/1"`; `results.summary` stores a
summary. `sim_study_list`, `sim_study_add`, `sim_study_edit` and `sim_study_delete`
provide persistence. Load without an id is accepted only when exactly one harness
exists. Save validates references and requires write confirmation; an unconfirmed edit
is not reported as saved. Loading reconstructs derived routing state.

```jsonc
{
  "schema": "aicad.harness/1", "id": "harness_id", "name": "Harness",
  "defaults": {"packing_factor": 0.6944444444, "bend_radius_x_od": null,
               "clearance_mm": 2.0, "service_loop_mm": 0.0, "strip_mm": 0.0},
  "packing_factor_basis": "estimate",
  "connectors": [], "wires": [], "nodes": [], "segments": [],
  "keepouts": [], "loads": [], "geometry": {}, "analyses": {}
}
```

Distances and coordinates are world mm, angles degrees; vectors are `[x,y,z]`.
Defaults may be overridden. Zero service-loop or strip allowance means unspecified,
not evidence that no allowance is needed. `geometry` and `analyses` hold generated
geometry and analysis state; edits clear stale `analyses`. `loads` is available for
load records; electrical checks also accept explicit loads and do not persist their
results. Keepouts supplied to routing are retained; forms are described in
[worker RPC](worker_rpc.md#harness-routing-and-path-checks).

## 2. Connectors and wires

A connector has `id`, unique `ref`, canonical `family`, `family_label`, `pins`,
`frame: {origin, normal, up}` and `cavities: [{pin, signal, wire}]`; optional fields
are `part_number`, `pitch_mm`, `rows`, `variant`, `gender`, `component`, `attach`
and `exit_dir`. Cavities start numbered as strings from `1`; the wire field identifies
attached wire(s). One wire per cavity is the default; deliberate shared pins require
`allow_shared_pin`. Pin counts are 1–256. Family common counts warn rather than reject.
The frame origin is the wire exit; normal is the outward mounting-face normal;
`exit_dir` defaults to normal and points into free space. Normal and up form the
normalized frame. The connector routing node has the same id and origin.

Canonical families: `jst_ph`, `jst_xh`, `jst_gh`, `jst_sh`, `molex_microfit_3`,
`molex_minifit_jr`, `header_2p54`, `dsub_de9`, `deutsch_dt`, `m8`, `m12_a`,
`m12_b`, `m12_d`, `m12_x`, `phoenix_mstb_5p08`, `phoenix_mc_3p5`,
`terminal_block`, `generic`. Family table entries describe pitch and common pin
counts, not housing envelopes. Terminal block pitch must be supplied; a supplied pitch
for a fixed-pitch family must agree with its table.

Each wire has `id`, `from` and `to` endpoint objects `{connector, pin, treatment?}`,
`insulation_od_mm`, `kind`, `length_mm`, `service_loop_mm`, `strip_mm`,
`min_bend_radius_mm`, `bend_radius_basis`, `bend_radius_confidence`, and `segments`
(segment ids). Endpoints can be supplied as `REF:pin` and resolve to connector ids;
they must be on different connectors. Supply `gauge_awg` or `csa_mm2`, and actual
insulated OD; the latter cannot be inferred from conductor gauge. Optional fields:
`insulation`, `color`, `net`, `signal`, `group`, `signal_class`,
`shield_ground_end`, `temp_rating_c`, `stranded`, `conductor_class`,
`r20_ohm_per_km`, `insulation_thermal_conductivity_w_mk`, `emissivity`,
`part_number`, `notes`.

| Field | Values |
|---|---|
| `kind` | `wire`, `twisted_pair`, `shielded`, `coax`, `ribbon` |
| `signal_class` | `power`, `signal`, `data`, `shielded_drain`, `ground` |
| `shield_ground_end` | `from`, `to`, `both`, `none`; only shielded/coax or shielded drain |
| endpoint `treatment` | `crimp`, `ferrule`, `solder`, `tinned`, `bare` |

Ribbon requires positive `ribbon_width_mm` and `ribbon_thickness_mm`; its routing OD
is their hypotenuse. It is routed as a circular envelope, not an oriented rectangular
sweep. Each wire record is one conductor/connection; grouped cable construction must
be supplied rather than inferred.

## 3. Routing state

Nodes are `{id, kind, pos}`, with kind `connector`, `breakout`, `clamp`, `waypoint`
or `splice`. A clamp is a node, not a separate clamp list: `harness_edit` splits a
segment at a strictly interior distance or on-path point. Segments are
`{id, a, b, path, od_mm, min_bend_radius_mm, wires, length_mm}`; `path` is a dense
polyline ordered from node `a` to `b` with endpoints snapped to node positions.
Optional segment fields include `sleeve_id_mm`, `override_od_mm`,
`override_packing_factor`, `override_min_bend_radius_mm` and derived OD and bend
basis/confidence fields. The graph is a forest; wire paths use the unique tree path.
Recomputation rebuilds segment membership, wire routes and lengths, bundle ODs and
bend estimates. An unrouted wire has `length_mm: null`, not zero. Cut length is routed
length plus service loop plus `strip_mm`, the total termination allowance for both ends.
All bend radii and caller multipliers refer to the centerline.

The read-only engine RPC `harness_route` plans a centerline using endpoint positions
and outward directions, OD, radius, clearance, keepouts and optional routing controls;
`harness_path_check` checks length, local bends and clearance. See the exact RPC
parameters and limitations in [worker_rpc.md](worker_rpc.md#harness-routing-and-path-checks).
Clearance uses world display tessellation, not exact B-rep; no obstacle is not a
measured clearance. The MCP router uses those RPCs and persists the resulting graph.

## 4. MCP surface

`services/mcp/harness_tools.py` registers:

| Tool | Purpose |
|---|---|
| `harness_new` | Create a named harness study. |
| `harness_get` | Read a harness, or list with `harness_id="*"`. |
| `harness_add_connector` | Add a connector with world frame or face attachment. |
| `harness_add_wire` | Add a fully specified pin-to-pin wire. |
| `harness_connect` | Convenience pin-to-pin wire creation. |
| `harness_from_netlist` | Seed wires from explicit schematic endpoint mapping. |
| `harness_route` | Route wires through the engine, reusing and splitting the forest. |
| `harness_check` | Check connectivity, geometry, fill, exits, segregation and shielding; optional sampled motion. |
| `harness_build_geometry` | Build sweep bodies or a persistent assembly harness component. |
| `harness_report` | Read cut list, lengths, BOM and connector tables. |
| `harness_formboard` | Write SVG, DXF or PDF formboard and report overlap/length verification. |
| `harness_edit` | Edit wire/default/segment overrides, remove wire or add clamp. |

`services/mcp/harness_electrical_tools.py` registers read-only tools:

| Tool | Purpose |
|---|---|
| `harness_electrical_check` | Screen current, voltage drop, thermal/ampacity and protection from supplied evidence. |
| `harness_bus_check` | Screen I2C, CAN, CAN FD, RS485, UART or SPI signal integrity. |
| `harness_cable_model` | Estimate cable R/L/C, impedance and delay from construction or datasheet inputs. |
| `harness_to_bus_links` | Convert routes and complete templates into firmware simulation bus links. |

Outputs are bounded/paged. Formboard drawing-sheet integration is not supported.

## 5. Sources and scope

- Static bend estimates cite [NASA-STD-8739.4A §7.2.19 Table 7-1](https://s3vi.ndc.nasa.gov/ssri-kb/static/resources/nasa-std-8739.4a.pdf) and the [inside-radius convention](https://pickeringconnect.com/blog/minimum-bend-radius/): individual polyimide 10× OD, other individual wires 6× OD (extrapolated where NASA has no generic individual-wire row); ribbon 2× envelope inside radius per the cited IPC/WHMA report. Bundles use 10× for polyimide, 6× for coax/large/unknown conductor, otherwise 3×; centerline adds half OD and accommodates member radii. These are low-confidence installed static screens, not dynamic-flex or workmanship certification; use actual cable datasheets.
- Bundle estimates cite [RapidHarness](https://rapidharness.com/support/design-types/harnesses/bundle-diameters) and [One Pull Wire](https://onepullwire.com/5-steps-to-calculate-the-diameter-of-bundled-cable/). Default area packing factor is `1/(1.2**2)`, an example rather than universal packing; coverings and grouped/twisted construction are not inferred. Override from measurement or datasheet.
- `services/harness/wire_data.py` cites ASTM B258 for AWG, ASTM B3/B193 and IEC 60028 for copper properties, IEC 60228 for metric resistance, NEC 310.16 and 310.15 for building-wire ampacity/corrections, and NASA MSFC-STD-3012A for its stated spaceflight conditions. NEC tables are limited to their specified conditions and 14 AWG and larger; neither thermal estimates nor spaceflight tables certify chassis wiring.
- The same file's insulation table cites Paragon's wire chart, Midland fluoropolymer data, Testo emissivity, and the material-specific references recorded with each table entry. PVC, PTFE, FEP, ETFE, XLPE, silicone and TPE values are representative screening values, not specifications for an unidentified wire. Emissivity varies with surface and temperature; actual continuous rating and formulation require manufacturer data. Unknown/ambiguous material properties remain unknown.
