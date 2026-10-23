# Contract: `aicad.civil/1` — civil / site / utility infrastructure model

> **Frozen seam.** Append-only; changes require maintainer sign-off + a version bump
> (see `contracts/README.md`). Python package: `services/civil/`; MCP surface:
> `services/mcp/civil_tools.py` (`civil_*` tools, registry family `sim`).

Civil engineering and infrastructure planning: CRS-aware site context, terrain (TIN/DEM),
alignments and profiles, pressurized water distribution, gravity sewer/storm networks,
hydrology and detention, culverts, buried-pipe structural checks, thrust restraint,
above-ground pipe supports, quantity takeoff / cost / schedule, and the couplings that tie a
site network to Chudware's process piping (`aicad.piping/1`), FEA and requirements.

## 1. Units and coordinates

- **SI metres everywhere in the model**: lengths/elevations/diameters m, areas m², volumes
  m³, flows m³/s, heads m, pressures kPa (only where a pressure is the natural quantity),
  time s, rainfall depth mm and intensity mm/h (hydrology inputs only). This differs from
  `aicad.piping/1` (mm) on purpose — civil drawings and engines work in metres; the
  piping coupling (§7) converts explicitly.
- Plan coordinates: `x` = easting, `y` = northing in the **project CRS** (projected, metres);
  `z` = elevation in the project vertical datum. Angles in the model are radians unless a
  field name ends in `_deg`. Azimuths/bearings are not stored; headings are math angles
  (CCW from +x).
- Import/export adapters convert foreign units (US survey ft, ft, gpm, cfs, in/h) at the
  boundary and report the conversion in `provenance`.

## 2. Results and refusals

- Every analysis function returns a JSON-serializable `dict` with `ok: true` plus
  `warnings: [str]`, `assumptions: [str]`, and `method: {name, reference}` (the governing
  equation/standard, edition when known). Numbers carry units in their key (`_m`, `_m3s`,
  `_kpa`, `_s`, `_mm`, `_pct`) or in an adjacent `units` map.
- Invalid input or an unsupported case raises `services.civil.errors.CivilError(reason,
  hint, **extra)`; the MCP layer returns `CivilError.to_dict()`. Never a silent clamp,
  default substitution, or fake success: a non-converged solve returns `ok: true` with
  `converged: false` and the residual, or raises `not_converged` — never claims convergence.
- Design-criteria checks return rows `{check, element_id, value, limit, units, pass,
  criterion, reference}`; jurisdictional limits come from a criteria profile (§6), never a
  hidden constant.

## 3. Site model document

```json
{
  "schema": "aicad.civil/1",
  "name": "Site A",
  "crs": {
    "horizontal": "EPSG:26915",
    "vertical": "EPSG:5703",
    "local": null,
    "notes": ""
  },
  "surfaces": [],
  "alignments": [],
  "water_networks": [],
  "gravity_networks": [],
  "catchments": [],
  "structures": [],
  "tie_ins": [],
  "criteria": {"profile": "ten_states_2014", "overrides": {}},
  "provenance": []
}
```

- `crs.horizontal`/`crs.vertical`: authority:code strings or `null` (unknown → GIS export
  refuses `crs_unknown`). `crs.local` (optional): `{origin_x, origin_y, rotation_rad,
  scale_factor}` mapping a local engineering grid onto the projected CRS:
  `X = origin_x + s·(x cosθ − y sinθ)`, `Y = origin_y + s·(x sinθ + y cosθ)`.
- `provenance`: `[{source, kind, imported_at, crs, units_in, notes}]` per import.
- All element `id`s are unique within their collection and stable across edits.

### 3.1 Surfaces (TIN)

```json
{"id": "eg", "name": "Existing ground", "kind": "tin",
 "points": [[x, y, z], ...], "triangles": [[i, j, k], ...],
 "breaklines": [[[x, y, z], ...], ...], "boundary": [[x, y], ...] | null,
 "source": {"kind": "points|dem|landxml|grading", "ref": "..."}}
```

Triangles are CCW in plan, index `points`. Breakline segments are constrained edges of the
triangulation (every breakline segment is a union of triangle edges).

### 3.2 Alignments

```json
{"id": "a1", "name": "Main St CL", "start_station": 0.0,
 "horizontal": [
   {"type": "line",   "start": [x, y], "end": [x, y]},
   {"type": "arc",    "start": [x, y], "end": [x, y], "center": [x, y], "radius": 150.0, "dir": "ccw"},
   {"type": "spiral", "start": [x, y], "start_heading": 0.12, "length": 60.0,
    "radius_start": null, "radius_end": 150.0, "dir": "cw"}
 ],
 "vertical": {"pvis": [{"station": 0.0, "elevation": 101.2, "curve_length": 0.0}, ...]} | null}
```

Elements are contiguous (end of one = start of next within 1e-6 m). `radius_* = null`
means infinite (tangent). Vertical curves are symmetric parabolas centred on the PVI.

### 3.3 Water (pressurized) networks

```json
{"id": "w1", "name": "Distribution", "headloss": "H-W|D-W",
 "junctions":  [{"id", "x", "y", "elevation", "demand_m3s", "pattern": null}],
 "reservoirs": [{"id", "x", "y", "head", "pattern": null}],
 "tanks":      [{"id", "x", "y", "elevation", "init_level", "min_level", "max_level", "diameter"}],
 "pipes":      [{"id", "from", "to", "length", "diameter", "roughness", "minor_loss": 0.0,
                 "status": "open|closed|cv", "material": null, "vertices": [[x, y], ...]}],
 "pumps":      [{"id", "from", "to", "curve": [[q_m3s, head_m], ...], "speed": 1.0, "status": "open"}],
 "valves":     [{"id", "from", "to", "type": "PRV|PSV|FCV|TCV", "diameter", "setting", "minor_loss": 0.0}],
 "patterns":   {"<id>": [multiplier, ...]},
 "options":    {"duration_s": 0, "hydraulic_step_s": 3600, "pattern_step_s": 3600,
                "demand_model": "DDA|PDA", "p_min_m": 0.0, "p_req_m": 20.0, "pda_exponent": 0.5,
                "viscosity_m2s": 1.004e-6, "accuracy": 1e-6, "max_iter": 200}}
```

`roughness` is the Hazen-Williams C for `H-W`, absolute roughness in **m** for `D-W`.
`from`/`to` reference any node id (junction/reservoir/tank). Pumps: 1-point curve is
EPANET's design-point form (shutoff 1.33·h at q=0, max flow 2·q); 3-point fits
`h = a − b·q^c`; more points are piecewise-linear.

### 3.4 Gravity (sewer / storm) networks

```json
{"id": "g1", "name": "Sanitary", "kind": "sanitary|storm|combined",
 "nodes":    [{"id", "type": "manhole|inlet|junction|outfall", "x", "y", "rim", "invert",
               "diameter": 1.2, "inflow_m3s": 0.0, "catchment": null, "loss_k": null,
               "outfall_tailwater": null}],
 "conduits": [{"id", "from", "to", "shape": "circular|rectangular|trapezoidal",
               "diameter": 0.3, "width": null, "height": null, "side_slope": null,
               "length": null, "n": 0.013, "inlet_invert": null, "outlet_invert": null,
               "material": null, "vertices": [[x, y], ...]}]}
```

`length` null = plan length from node xy (+vertices). Conduit inverts null = node invert.
Flow direction is `from → to`.

### 3.5 Catchments

```json
{"id": "c1", "name": "Lot 4", "outlet": "<gravity node id>", "area_m2": 12000.0,
 "runoff_coeff": 0.55, "cn": 78, "imperv_pct": 40.0, "polygon": [[x, y], ...] | null,
 "tc_s": null, "tc_segments": [{"type": "sheet|shallow|channel", "length", "slope", ...}]}
```

### 3.6 Structures

`{"id", "kind", ...}` with kinds:
`detention_pond` (`stage_storage: [[stage_m, volume_m3]...]`, `outlets: [{type:
orifice|weir|riser, ...}]`, `inflow_node`, `outflow_node`), `culvert` (HDS-5 inputs, §4
`services.civil.culvert`), `thrust_block` (`network`, `node`, `bearing_kpa`, result
dimensions), `pipe_support` (`piping_doc_id`/`run_id`, station, type).

### 3.7 Tie-ins (site network ↔ process piping)

```json
{"id": "t1", "civil_network": "w1", "civil_node": "J12",
 "piping_doc_id": "<doc>", "piping_study": "piping_network",
 "piping_endpoint": {"run_id": "R1", "end": "start|end"},
 "direction": "supply|discharge", "elevation_offset_m": 0.0}
```

`supply`: the site network supplies the plant (civil head → piping inlet pressure; piping
flow → civil junction demand). `discharge`: plant discharges to the site network (piping
outlet flow → civil inflow).

## 4. Python modules (`services/civil/`)

| module | owns |
|---|---|
| `errors.py` | `CivilError` |
| `schema.py` | `new_model(name, crs=None)`, `validate(model) -> [str]`, element factories, id/collection helpers, `find(model, collection, id)` |
| `crs.py` | CRS records, local-grid ↔ projected transform, pyproj transforms (projected ↔ WGS84, between EPSG codes) with provenance; grid/combined scale factor |
| `gis_io.py` | GeoJSON (RFC 7946, WGS84 lon/lat on export) + GeoPackage (OGC, SQLite, project CRS) import/export of model elements |
| `landxml.py` | LandXML 1.2 import/export: CgPoints, Surfaces (TIN), Alignments (CoordGeom Line/Curve/Spiral + Profile/ProfAlign PVI/ParaCurve), PipeNetworks (Struct/Pipe) |
| `ifc_export.py` | IFC 4.3 (IFC4X3_ADD2) STEP-file writer: IfcProject/IfcSite/IfcMapConversion, IfcAlignment (horizontal segments), IfcPipeSegment/IfcDistributionChamberElement in IfcDistributionSystem |
| `terrain.py` | constrained TIN, `z_at`, contours, slope/aspect, surface-surface cut/fill volume, stage–storage under a polygon, pad grading with daylight |
| `dem.py` | grid DEM (ESRI ASCII grid / XYZ), DEM→TIN, depression fill, D8 flow direction/accumulation, watershed delineation, stream extraction |
| `alignment.py` | horizontal/vertical geometry, station↔XY, station/offset, profile sampling (`z_at` callable), cross-sections, average-end-area / prismoidal earthwork, mass haul, sight-distance/K checks |
| `water.py` | GGA (Todini–Pilati) steady + extended-period solve, H-W/D-W, pumps/valves/tanks, PDA, fire-flow analysis |
| `epanet_inp.py` | EPANET `.inp` import/export (SI and US units) |
| `gravity.py` | partial-flow conduit geometry, Manning normal/critical depth, network capacity design, HGL/EGL with junction losses, cover/slope/velocity criteria checks, open-channel GVF standard step, hydraulic jump |
| `swmm_inp.py` | SWMM 5 `.inp` import/export (junctions/outfalls/conduits/xsections/subcatchments) |
| `hydrology.py` | rational method, NRCS CN runoff, TR-55 Tc, NRCS unit hydrograph, design storms (SCS type I/IA/II/III, user IDF tables), modified Puls routing, outlet structures |
| `culvert.py` | FHWA HDS-5 inlet- and outlet-control headwater, controlling HW, overtopping flag |
| `buried.py` | prism/Marston earth load, live load from explicit wheel load distribution, modified Iowa deflection, ring buckling, flotation, thrust force + thrust block sizing, restrained joint length |
| `supports.py` | above-ground pipe support spacing (stress + deflection limits), support reactions, rack loads from an `aicad.piping/1` network |
| `planning.py` | quantity takeoff (pipe by size/material, trench excavation/bedding/backfill, structures, earthwork), unit-price cost estimate from a user price book, CPM schedule, element↔task 4D links |
| `criteria.py` | criteria profiles (`ten_states_2014`, `custom`), override merge, lookup |
| `piping_link.py` | tie-in coupling: civil water solve ↔ `aicad.piping/1` solve fixed-point iteration through an injected piping solver; `aicad.simhandoff/1` envelopes (couplings `civil_to_piping`, `piping_to_civil`, `civil_to_fea`) |

Cross-module calls take **callables or plain dicts**, never module-private state: e.g.
`alignment.profile(alignment, z_at)` takes `z_at(x, y) -> float | None` (from
`terrain.surface_sampler(surface)`), `gravity.check_cover(network, z_at, criteria)`.

## 5. Persistence

One document study per civil model, owned by `services/mcp/civil_tools.py`:

```json
{"type": "civil_site", "name": "<model name>",
 "params": {"model": <aicad.civil/1>},
 "results": {"<analysis key>": <analysis result>, ...}}
```

Analysis keys: `water`, `fire_flow`, `gravity`, `hydrology`, `detention`, `culvert`,
`earthwork`, `buried`, `thrust`, `supports`, `qto`, `cost`, `schedule`, `tie_ins`.
A model edit clears results whose inputs changed (the tool layer decides; stale results are
never shown as current). `civil_load` restores the study after reopen.

## 6. Criteria profiles

`criteria.py` ships `ten_states_2014` (GLUMRB Recommended Standards for Wastewater
Facilities, 2014 §§33.1–34.1, and Recommended Standards for Water Works 2018 §8.2.1) and
`custom` (same keys, editable). Values are **exact conversions of the customary-unit
text**: 8 in = 0.2032 m minimum sanitary diameter, 2 ft/s = 0.6096 m/s full-flow minimum
velocity, minimum slopes per nominal inch diameter (8 in 0.40 % … 36 in 0.046 %), manhole
spacing 400 ft = 121.92 m (≤ 15 in) / 500 ft = 152.4 m (18–30 in); water 20 psi head under
all flow conditions (`water_min_pressure_m`) and 35 psi normal working pressure
(`water_normal_min_pressure_m`). Values that are practice rather than the standard are
labelled `practice` in `references`. Solvers never hard-code a limit.

## 7. Couplings (`aicad.simhandoff/1`)

| coupling | source → target | conditions |
|---|---|---|
| `civil_to_piping` | civil water solve → piping network | inlet head/pressure at each supply tie-in (`kpa`, piping endpoint as `applied_to`) |
| `piping_to_civil` | piping solve → civil water network | flow demand at each tie-in junction (`m3/s`) |
| `civil_to_fea` | thrust/buried result → FEA static study | thrust force vector on block bearing face, soil pressure on pipe crown |

Verify envelopes compare tie-in head/flow agreement after the coupled iteration
(tolerance default 1 %).

Coupling scope: `services.civil.piping_link.couple` is steady-state demand-driven only
(EPS/PDA/patterned tie-in demands refuse by name). An omitted fluid is passed explicitly and
disclosed as fresh water (ρ = 1000 kg/m³, μ = 0.001004 Pa·s). Supply exchange is a signed
junction demand added to the base demand; discharge is a signed inflow.

## 8. Field clarifications and optional extensions (round 1)

Optional fields below are additive; absent means the stated default.

- **Survey points:** optional top-level `points: [{id, x, y, z, code?, desc?}]` (LandXML
  CgPoints) retained for exchange; not used by analyses.
- **Incomplete imports:** geometry-only imports (LandXML, GIS) may leave hydraulic
  coefficients/loads/status `null`; analyses refuse until the user completes them —
  nothing is guessed.
- **Water:** `junction.demands: [{demand_m3s, pattern}]` (multiple categories, summed),
  `network.curves: {id: [[x, y]...]}`, `pump.curve_id`, `pump.speed_pattern`,
  `tank.min_volume_m3|volume_curve|overflow` (non-cylindrical/overflow tanks refuse in the
  solver), `valve.status: active|open|closed` (default `active`; `open` bypasses
  regulation), simple tank-level and elapsed-time `controls`, `options.default_pattern |
  demand_multiplier | pattern_start_s`; EPANET import metadata (`epanet_units`,
  `pressure_units`, `coordinate_units`, `epanet_map`, `passthrough`) preserved for exact
  round trip. Fire flow is an instantaneous residual-pressure test, not an endurance test.
- **Gravity:** `node.outfall_tailwater` is a **depth above the outfall invert** (m);
  rectangular conduits take `closed: bool` (default `false` = open; SWMM `RECT_CLOSED`
  sets `true`); `conduit.allow_adverse_slope: bool` (default `false` → `adverse_slope`
  refusal); `network.swmm_options` and `swmm_*` element fields preserve SWMM source data.
  HGL/EGL is preliminary steady Manning + K screening (HEC-22 4th ed. §9.1.6.6 caveat),
  not the FHWA energy-loss method or dynamic-wave routing.
- **Structures:** `detention_pond.outlets[].type` adds `rating_curve` with
  `stage_discharge: [[stage_m, q_m3s]...]`; orifice `{cd, diameter_m, invert_m}`, weir
  `{cd, crest_m, shape: rectangular (width_m) | v_notch (angle_deg)}`, riser `{cd,
  orifice_cd, diameter_m, crest_m}`, optional `tailwater_m`. `culvert`: `{shape: circular
  (diameter_m) | rectangular (width_m, height_m), length_m, slope, n, inlet (see
  culvert.inlet_types()), barrels=1, ke?, inlet_invert_m?, road_crest_m?, tailwater_m?}`;
  tailwater is depth above the outlet invert.
- **Terrain/alignment results:** `terrain.stage_storage` returns the §2 envelope with a
  `stage_storage: [[stage_m, volume_m3]]` table. Grading slopes are positive H:V ratios;
  aspect is the downhill direction, CCW from +x. `alignment.cross_sections` returns
  `{sections, mid_sections, element_ids}`; shrink = compacted/bank, swell = loose/bank;
  earthwork totals are `total_cut_m3`/`total_fill_m3` (bank / geometric compacted).
  Vertical criteria overrides `ssd_m`, `crest_k_m_per_pct`, `sag_k_m_per_pct` are
  accepted by `alignment.vertical_curve_checks` only.
- **Planning:** trench QTO needs explicit pipe outside diameter (`conduit.outer_diameter_m`
  or a trench `outer_diameter_by_material` map) — hydraulic ID is never used as OD; water
  pipes may carry `start_elevation_m`/`end_elevation_m`; price books are user data only.
- **Network sub-collections are optional:** an absent or `null` `junctions`, `reservoirs`,
  `tanks`, `pipes`, `pumps`, `valves`, `patterns`, `curves`, `options` (water) or `nodes`,
  `conduits` (gravity) means empty. `water.solve` needs ≥ 1 source (reservoir/tank) and
  ≥ 1 link; gravity analysis needs ≥ 1 node — each refuses by name otherwise.
- **Null == absent** for every optional field; a supplied non-null value is strictly typed.
