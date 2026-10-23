# Contract: `aicad.piping/1` document schema

> **Frozen seam.** Append-only; changes require maintainer sign-off + a version bump
> (see `contracts/README.md`). This is the executable-backed schema for the piping
> discipline module, a sibling of `engine.doc` / `engine.ecad`. The executable form is
> `services/piping/schema.py` (structural validation only — geometry realizability and
> catalog resolution are separate passes in `services/piping/catalog.py` +
> `services/piping/threed.py`, exactly as ECAD keeps DRC/3D out of its schema validator).

## 1. Scope & conventions

A piping document describes a routed pipe assembly: one or more **pipe runs** (a swept
solid pipe following a 3-D polyline centreline) plus discrete **fittings**
(elbow / tee / reducer / flange) placed in the same coordinate frame.

- **Units:** millimetres internally, everywhere. Angles in **degrees**.
- **Coordinates:** right-handed `[x, y, z]` in mm. Path points are 3-D.
- **Sizes:** nominal pipe size (**NPS**, inches) + a **schedule** name resolve to real
  wall/OD via the MIT-licensed `fluids.piping.nearest_pipe` (see `catalog.py`). ASME
  B36.10M/B36.19M dimensional data is obtained at runtime from `fluids`, never bundled.
- **Errors:** validators return a `list[str]` of JSON-path-prefixed messages (empty =
  valid), matching `engine.doc.schema` / `engine.ecad.schema`. Runtime catalog / geometry
  failures degrade **contract-shaped** (`{"ok": false, "reason": ..., "hint": ...}`),
  never as tracebacks.

## 2. Enumerations (frozen)

| enum | members |
|---|---|
| `SCHEDULES` | `5 5S 10 10S 20 30 40 40S 60 80 80S 100 120 140 160 STD XS XXS` |
| `FITTING_TYPES` | `elbow tee reducer flange` |
| `ELBOW_ANGLES` (deg) | `45 90` |
| `ELBOW_RADII` | `LR` (long-radius, R = 1.5·DN) · `SR` (short-radius, R = 1.0·DN) |
| `FLANGE_CLASSES` | `150 300 600 900 1500 2500` |
| `FLANGE_FACES` | `RF` (raised) · `FF` (flat) · `RTJ` (ring-type joint) |
| `MATERIALS` | `A106-B A53-B A312-304 A312-316 A333-6 PVC CPVC copper` (advisory; drives colour + density, not geometry) |
| `END_TYPES` | `BW` (buttweld) · `SW` (socket-weld) · `THD` (threaded) · `FLG` (flanged) |

`STD`/`XS`/`XXS` are aliases resolved by `fluids` (`STD`≈40 through 10", `XS`≈80). NPS is a
number in inches (`0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4, 6, 8, 10, 12, …`); it is stored
as-is and snapped to the nearest catalogued size by `catalog.nearest_pipe`.

## 3. Document envelope

```jsonc
{
  "schema": "aicad.piping/1",
  "id": "<uuid>",
  "name": "Skid line 100",
  "units": { "system": "MMGS" },
  "material": "A106-B",          // document default; runs/fittings may override
  "runs":     [ <pipe_run>, ... ],
  "fittings": [ <fitting>,  ... ]
}
```

`id` is a UUID. `runs` and `fittings` are ordered lists; ids are unique within their list
and use the prefixes below (`next_id` allocates, never reuses).

| list | id prefix |
|---|---|
| `runs` | `r` |
| `fittings` | `ft` |

## 4. `pipe_run`

```jsonc
{
  "id": "r1",
  "name": "Header",
  "suppressed": false,
  "path": [ [0,0,0], [1000,0,0], [1000,800,0], [1000,800,600] ],  // >=2 pts, mm
  "nps": 2,                    // nominal pipe size, inches
  "schedule": "40",            // member of SCHEDULES
  "material": null,            // null = inherit document default
  "bend_radius": null,         // mm; null => long-radius default = 1.5 * outer-dia
  "end_type": "BW"             // member of END_TYPES
}
```

- `path` is the centreline as a 3-D polyline; consecutive points must differ. Interior
  vertices are rounded to `bend_radius` (tangent circular arcs) when swept — the geometry
  builder honours the pipe bend radius so elbows are real, not mitred (`threed.py`).
- `nps` + `schedule` are resolved to `(outer_dia, inner_dia, wall)` mm at build time.
- A run with `< 2` path points, zero-length segments, or an unresolvable `nps`/`schedule`
  is a validation / build error (contract-shaped at build time).

## 5. `fitting`

```jsonc
{
  "id": "ft1",
  "type": "elbow",             // member of FITTING_TYPES
  "name": "E1",
  "suppressed": false,
  "origin": [1000, 0, 0],      // placement point, mm
  "orient": [0, 0, 0],         // XYZ Euler angles, degrees
  "nps": 2,                    // primary size (large end for a reducer)
  "nps2": null,                // reducer small end; required iff type == "reducer"
  "schedule": "40",
  "material": null,
  "catalog_ref": null,         // optional catalog key override; null => derived from type+nps
  "params": {                  // type-specific, all optional (catalog fills defaults)
    "angle": 90,               // elbow: member of ELBOW_ANGLES
    "radius": "LR",            // elbow: member of ELBOW_RADII
    "flange_class": 150,       // flange: member of FLANGE_CLASSES
    "flange_face": "RF",       // flange
    "branch_nps": null         // tee: branch size; null => same as nps
  }
}
```

Per-type required `params` (missing keys take catalog / default values, they are not
hard errors — only *wrong* values are):

| type | uses | notes |
|---|---|---|
| `elbow` | `angle`, `radius` | centre-to-end from B16.9; radius LR = 1.5·DN, SR = 1.0·DN |
| `tee` | `branch_nps` | equal or reducing tee; run + branch cylinders |
| `reducer` | `nps2` (top-level) | concentric frustum; length from B16.9 |
| `flange` | `flange_class`, `flange_face` | weld-neck proxy: hub + disc + bolt-circle holes |

## 6. Fitting catalog (`engine/piping/data/fittings.csv`)

A CSV of dimensional data for B16.9 buttweld fittings + B16.5 flanges, built from public
manufacturer catalogs (dimensions are facts, not copyrightable; ASME text is **not**
bundled). Columns:

```
type,nps,nps2,center_to_end,length,flange_class,od,thickness,bolt_circle,bolt_holes,bolt_dia
```

- `center_to_end` (mm): elbow/tee A or C dimension.
- `length` (mm): reducer end-to-end H.
- `od`, `thickness`, `bolt_circle`, `bolt_holes`, `bolt_dia`: flange disc geometry.
- Empty cells mean "not applicable to this row's type". `catalog.fitting_dims(type, nps,
  ...)` returns a row dict or a contract-shaped `{"ok": false, ...}` when absent, with a
  `hint` naming the nearest available size.

## 7. Realization pipeline (informative)

`services/piping/threed.py` turns an `aicad.piping/1` doc into geometry:

1. `piping_to_step(doc, out_path)` → OCCT sweep of each run's annular section along its
   filleted centreline + parametric fitting solids → one `Compound` → STEP on disk. Returns
   `{"step_path", "summary"}`.
2. `piping_to_part_doc(doc, step_path)` → a minimal `aicad.part/1` doc with a single
   `import` feature referencing the STEP — the same envelope ECAD's `board_to_part_doc`
   emits, so the host opens the routed pipe directly in the CAD viewport.

Geometry realizability (self-intersections, unresolved sizes) is reported in `summary`,
never raised — consistent with `contracts/ecad/fab_outputs.md §5`.

## 8. Engineering RPCs: `piping_route`, `piping_spec_check`, `piping_bom`

Native (`core/piping/route.cpp`, `spec.cpp`, `bom.cpp`; dispatched by
`chudware::piping::call`, reachable via `chudware_call` / `core_bridge.call`). All JSON,
lengths mm, pressures kPa gauge, temperatures °C. Success envelopes carry
`{ok: true, …, warnings: [], hint, next_actions}`; refusals are `{ok: false, reason, hint}`
with a snake_case `reason`.

### 8.1 `piping_route`

```jsonc
// params
{ "start": {"point": [x,y,z], "dir": [dx,dy,dz]?},   // dir = travel direction LEAVING start
  "end":   {"point": [x,y,z], "dir": [dx,dy,dz]?},   // dir = travel direction ARRIVING at end
  "nps": 2, "schedule": "40", "material": null,
  "obstacles": [{"min": [x,y,z], "max": [x,y,z], "name": "pump skid"}],
  "clearance_mm": 50, "grid_mm": null, "bend_penalty": null,
  "piping": null,            // existing aicad.piping/1 doc to append to (else a new doc)
  "name": "Routed run", "doc_id": null, "ignore_bodies": [] }
// result
{ "ok": true, "piping": <aicad.piping/1>, "run_id": "r3", "path": [[x,y,z], …],
  "length_mm": 1560.3, "bends": 4, "elbow_ids": ["ft5", …], "min_clearance_mm": 50.0,
  "nearest_obstacle": "pump skid", "clearance_mm": 50, "grid_mm": 60.3,
  "bend_penalty_mm": 180.9, "search": {"nodes", "expanded_states", "grid": [nx,ny,nz]},
  "warnings": [], "hint": "…", "next_actions": ["piping_spec_check", "piping_bom", "piping_solve"] }
```

- **Algorithm.** Bend-penalised A* over (grid node, travel direction) on a rectilinear grid:
  uniform lines every `grid_mm` (default `max(OD, 25)`) anchored at the start stub, plus the
  exact endpoint, stub-tip and inflated-obstacle-face coordinates, so a route hugs an
  obstacle at exactly the requested clearance. Obstacles are AABBs inflated by
  `clearance_mm + OD/2` (centreline configuration space). Cost = length + `bend_penalty`
  (mm of equivalent pipe; default `max(3·grid_mm, 2·A)`) per 90° turn; 180° fold-backs are
  forbidden. Grids over 10⁶ nodes are coarsened (warning; obstacle faces stay exact).
- **Nozzle stubs.** A given `dir` (snapped to the nearest axis, warning if it was oblique) puts
  a straight of at least one elbow take-out (B16.9 LR A = 1.5·NPS; catalog value) at that
  end, extended until it clears every clearance envelope. A nozzle with outward normal *n*
  takes `start.dir = n`, `end.dir = −n`. An endpoint inside a clearance envelope (but not a
  solid) without `dir` leaves along the shortest escape axis (warning); the stub is exempt
  from the clearance check against the envelope it escapes.
- **Output.** Collinear points merged; the new run (`bend_radius` = A, so the sweep matches
  the elbows) and one `elbow` fitting per bend (`params {angle: 90, radius: "LR"}`, `origin`
  at the corner, `orient` = intrinsic-XYZ Euler degrees carrying the canonical elbow — in
  along +X, out along +Y — onto the turn, the `threed.py` `_place` convention) are appended
  with `next_id` ids; the document passes `validate_piping`. Runs already in `piping` are
  obstacles too, except segments the new route starts/ends on.
- **`doc_id`.** The libchudware glue injects a world-AABB obstacle for every solid body of
  that document (minus `ignore_bodies`) before dispatch; `ignore_bodies` also filters named
  `obstacles`.
- **Refusals.** `no_route` (no collision-free path in the search domain; or a nozzle stub runs
  into a body), `start_inside_obstacle` / `end_inside_obstacle` (point strictly inside a
  solid; hint names it), `invalid_params`, `unknown_pipe_size`, `invalid_piping` (with
  `errors`), `route_domain_too_large`.

### 8.2 `piping_spec_check`

```jsonc
// params
{ "piping": <aicad.piping/1>, "design_pressure_kpa": 2000, "design_temp_c": 40,
  "corrosion_allowance_mm": 1.5, "mill_tolerance_frac": 0.125, "joint_factor": 1.0,
  "fluid_density_kgm3": 1000, "insulation_kgm": 0 }
// result
{ "ok": true, "code": "ASME B31.3", "design": {…, "Y": 0.4, "W": 1.0},
  "runs": [{ "id", "nps", "schedule", "material", "od_mm", "t_nominal_mm", "t_pressure_mm",
             "t_required_mm", "allowable_mpa", "mawp_kpa", "utilization", "support_span_m",
             "support_span_basis", "weight_operating_npm", "pass", "suggested_schedule"? }],
  "flanges": [{ "id", "nps", "flange_class", "group", "rating_kpa", "utilization", "pass" }],
  "all_pass": true, "warnings": [], "hint", "next_actions" }
```

- **Pressure design** (B31.3 §304.1.2): `t = P·D / (2·(S·E·W + P·Y))`, Y = 0.4, W = 1.0;
  `t_required = (t + c) / (1 − mill_tolerance)`; `pass` iff `t_nominal ≥ t_required`;
  `utilization = t_required / t_nominal`; `mawp = 2·S·E·W·t_a / (D − 2·Y·t_a)` with
  `t_a = t_nominal·(1 − mill) − c`. A failing run names the thinnest catalogued schedule that
  passes (`suggested_schedule`). `t ≥ D/6` or `P/(SE) > 0.385` warns (eq. 3a out of range).
- **Allowable stress S** (MPa): B31.3 Table A-1 for A106-B / A53-B / A333-6 (20.0 ksi to
  400 °F … 10.8 ksi at 800 °F); A312-TP304 / A312-TP316 use the BPVC II-D lower
  (no-deformation) row. Linear in temperature, capped at 800 °F (427 °C) →
  `temperature_out_of_range` above. Schema names `A312-304`/`A312-316` alias to the TP grades.
  Any other material refuses `unknown_material` naming it.
- **Flanges** (`type: flange`, `params.flange_class`, default 150): B16.5 Table 2 P-T rating,
  group 1.1 (carbon steel), 2.1 (304), 2.2 (316), interpolated in temperature (−29…538 °C);
  classes 900/1500/2500 = class-600 rating × class/600. Material from the fitting, else the
  run it sits on, else the document.
- **Support span**: simply supported, `min(L_δ, L_σ)` with `5wL⁴/(384EI) = 2.5 mm` and
  `wL²/(8Z) = S − P·D/(4·t_s)` (sustained limit less longitudinal pressure stress);
  `w` = pipe (nominal wall) + fluid + insulation weight; `I`, `Z` on `t_s = t_nominal − c`;
  E = 203.4 GPa carbon / 195.1 GPa stainless (ambient).
- **Refusals**: `invalid_params`, `invalid_piping`, `unknown_material`, `unknown_pipe_size`,
  `temperature_out_of_range`, `external_pressure_not_covered` (negative gauge pressure),
  `unknown_flange_class`.
- **Data sources** (checked 2026-10; supersede the example numbers above): carbon steel S from
  ASME B31.3-2024 Table A-1C — 20.0 ksi to 300 °F, 19.9 at 400 °F … 11.4 ksi at 800 °F;
  austenitic S is the BPVC II-D 2023 Table 1A lower row (B31.3 Table A-1C itself lists the
  higher row); flange ratings are ASME B16.5-2025 Tables 2-1.1 / 2-2.1 / 2-2.2 (bar). The
  result carries these editions in `source`.

### 8.3 `piping_bom`

```jsonc
// params
{ "piping": <aicad.piping/1>, "max_spool_length_mm": 12000 }
// result
{ "ok": true,
  "line_list": [{ "run_id", "name", "nps", "schedule", "material", "end_type",
                  "length_mm", "cut_length_mm" }],
  "items": [{ "kind": "pipe"|"elbow"|"tee"|"reducer"|"flange", "nps", "nps2"?, "schedule"?,
              "flange_class"?, "material", "unit": "m"|"ea", "qty", "length_mm"?,
              "unit_mass_kg", "mass_kg" }],
  "spools": [{ "id": "SP1", "run_ids", "fitting_ids", "length_mm", "shop_welds",
               "pieces": [{ "run_id", "from_mm", "to_mm" }] }],
  "totals": { "pipe_length_m", "centreline_length_m", "mass_kg", "shop_welds",
              "field_welds", "flanged_joints", "spools" },
  "max_spool_length_mm": 12000, "warnings": [], "hint", "next_actions" }
```

- **Pipe** items aggregate by (nps, schedule, material): `qty` = runs, `length_mm` = summed cut
  length (centreline minus fitting take-outs: elbow `R·tan(θ/2)` per side, tee C, reducer H/2,
  flange disc thickness), `unit_mass_kg` per metre from catalog OD/ID × density (7850 kg/m³
  carbon steel, 8000 stainless, 1400 PVC, 1550 CPVC, 8940 copper).
- **Fitting** masses are idealised solids of revolution on the fitting's schedule wall: elbows
  by Pappus (wall annulus × R·θ; LR R = A, SR R = ⅔·A), tees as run (2C) + branch
  (C − OD/2) tubes, reducers as a mean-wall frustum of length H, flanges as the B16.5 disc
  less bolt holes (hub excluded — a lower bound; classes not catalogued fall back to class-150
  dims with a warning). Uncatalogued sizes report `null` mass with a warning.
- **Spools.** Runs meeting at an endpoint (or a branch point, 1 mm snap) join one spool unless
  a flange sits at the joint (bolted); a flange pair at a joint takes one run each. Each
  connected set is packed in document order into spools of at most `max_spool_length_mm`
  centreline; a run cut at a spool boundary adds one field weld.
- **Welds** (butt/socket; threaded `end_type: THD` runs weld nothing): elbow 2, tee 3,
  reducer 2, flange 1 (weld-neck), a run-to-run joint without a fitting 1 (a branch without a
  tee warns). A weld joining two different spools is a field weld; all others are shop welds.
- **Refusals**: `invalid_params`, `invalid_piping`, `unknown_pipe_size`, `unknown_material`.

## 9. Interactive network editing and fitting loss models

### 9.1 `piping_edit`

The native editor is shared by viewport drawing and agent tools. Parameters are
`{piping?: <aicad.piping/1>|null, ops: [...], tol_mm?: 1.0}`. An absent/null document
starts an empty MMGS network. Operations execute against a private copy, in order:
either all operations succeed or the input document remains unchanged.

- `add_run`: `{op:"add_run", path:[[x,y,z],...], nps?, schedule?, material?,
  bend_radius?:"LR"|"SR", end_type?, name?}`. Requires at least two distinct points.
  Collinear interior points are removed. Size/schedule/material/bend defaults come
  from the first existing run, otherwise NPS 2, schedule 40, A106-B, LR. Stored
  `run.bend_radius` remains a physical radius in mm. Every drawn corner creates an
  elbow with its actual deflection angle (finite `0 < angle < 180` degrees).
  An endpoint within tolerance of an existing run interior splits that run and
  creates a tee. At existing endpoint nodes, angled two-run connections create
  elbows; three-run connections create tees using the most collinear pair as
  `params.run_ids:[upstream_id,downstream_id]` and the other as `params.branch_run`.
  A fourth connected run refuses `node_full`. Elbows and tees are real drawn
  geometry, not inline placeholder fittings.
- `split_run`: `{op:"split_run", run_id, point}` creates a new far-part run ID and
  reattaches fittings and tee references by position.
- `add_fitting`: `{op:"add_fitting", type, at:[x,y,z], run_id?, params?, nps?,
  nps2?, name?}` supports inline `valve`, `orifice`, `custom`, `reducer`, `flange`.
  Placement projects onto the named run, or the nearest run if omitted, within
  `max(tol_mm, pipe_OD_mm)`. Reducers require `nps2`, split the run, and set the
  downstream run's NPS to that outlet size.
- `update_fitting`: `{op:"update_fitting", id, params?, name?, nps2?}` merges
  parameter keys; a null value deletes a key. For a reducer, an outlet size in
  `nps2` or `params.nps2` updates the fitting's top-level `nps2` and its connected
  downstream run's NPS (and the downstream run's attributed fittings).
  Elbow `params.radius:"LR"|"SR"` changes its deterministic loss correlation and
  the owning run's physical bend radius, keeping the shaded body in sync.
- `remove_fitting`: `{op:"remove_fitting", id}`.
- `remove_run`: `{op:"remove_run", id}` drops its fittings. A surviving tee with
  two collinear same-spec runs merges those runs; an angled two-run connection
  becomes an elbow.
- `move_vertex`: `{op:"move_vertex", run_id, index, point:[x,y,z]}` uses a zero-based
  vertex index. Connected run endpoints follow a moved endpoint; elbow deflections
  are recomputed.
- `set_run`: `{op:"set_run", id, nps?, schedule?, material?, bend_radius?, name?,
  suppressed?}` updates the run and its attributed fitting specification.

Success is `{ok:true, piping:<updated document>, created:[ids], removed:[ids],
changed:[ids], warnings:[]}`. Refusal is `{ok:false, reason, hint, op_index}` where
`op_index` is zero-based. Reasons: `invalid_params`, `unknown_run`,
`unknown_fitting`, `degenerate_path`, `not_on_run`, `node_full`, `invalid_k_model`.
No partially edited document is returned on refusal.

### 9.2 Additional fittings, attribution, and `k_model`

Fitting types now also include:

- `valve`: `params.valve_type` is `gate`, `globe`, `angle`, `ball`, `plug`,
  `butterfly`, `check_swing`, or `check_lift`.
- `orifice`: `params.beta` is the bore-to-pipe-ID ratio (`0 < beta < 1`), or
  `params.bore_mm` specifies a positive bore smaller than the pipe ID.
- `custom`: requires `params.k_model`; `params.label` is optional.

Every fitting created by the editor has `run_id`, identifying the link to which
its K belongs. This reference must resolve to a run. Attributed tees additionally
have two distinct `params.run_ids` and `params.branch_run`: branch loss belongs
only to `branch_run`, and through-run loss only to `run_ids[1]`, never also the
upstream link. `piping_solve`, pressure-drop reports, and `run_loss_1d` (the
`pipe_flow` callback) share this attribution and evaluator. Legacy fittings
without `run_id` retain their origin-on-polyline, first-matching-run placement
and legacy tee constants.

Any fitting may override its correlation with `params.k_model`:

| Model | Parameters / expression |
|---|---|
| `constant` | `{kind:"constant",K}` |
| `two_k` | `{kind:"two_k",K1,Kinf}`; Hooper `K = K1/Re + Kinf*(1+1/D_in)` |
| `three_k` | `{kind:"three_k",K1,Ki,Kd}`; Darby `K = K1/Re + Ki*(1+Kd/D_in^0.3)` |
| `cv` | `{kind:"cv",Cv}`; US gpm/sqrt(psi), `K = 890.3*D_in^4/Cv^2` |
| `table` | `{kind:"table",points:[{re,K},...],source:"cfd"|"user",fit?:{K1,Kinf},provenance?:{...}}` |

`D_in` is pipe inner diameter in inches. Coefficients and K must be finite and
nonnegative; Cv must be positive. Tables require at least one point with finite,
positive, strictly increasing Reynolds numbers and nonnegative K values. Table
evaluation interpolates K linearly in log(Re), clamping at both ends; optional
`fit` is nonnegative two-K metadata, not a replacement for the table.

Deterministic valve correlations are Crane TP-410 multiples of clean commercial
steel fully turbulent friction factor:
`fT = [-2*log10((0.0457 mm / ID_mm)/3.7)]^-2`.
Multipliers: gate 8, globe 340, angle 150, ball 3, plug 18, butterfly 45 at
NPS <=8, 35 at 10–14, 25 at >=16, swing check 100, lift check 600. Attributed
tee run/branch multipliers are 20/60. Elbows retain the Rennels rounded-bend
correlation, optionally using positive `params.radius_mm` for an explicit sweep
radius. Sharp-edged orifices use the permanent-loss correlation described by
Rennels and Hudson, *Pipe Flow: A Practical and Comprehensive Guide*:
`K = (sqrt(1-beta^4*(1-C^2))/(C*beta^2)-1)^2`, with `C=0.61`.
Every evaluated loss carries its `method` string.

### 9.3 `piping_fitting_k`

Native deterministic preview accepts
`{fitting:{type,params,nps?,nps2?}, nps?, schedule?:"40", reynolds?,
velocity_ms?, fluid?}`. Top-level NPS overrides fitting NPS; the default is NPS 2.
Explicit Reynolds takes precedence; otherwise velocity and resolved fluid
properties determine it, or it defaults to `1e5`.

Success: `{ok:true,K,method,fT,inner_dia_mm,reynolds,
curve:[{re,K},...]}`. The curve has 24 log-spaced Reynolds points from `1e3`
through `1e7` and uses exactly the solver's fitting evaluator. Invalid custom
or override models refuse `invalid_k_model`; malformed fitting/size/flow inputs
refuse `invalid_params`.

### 9.4 Per-path tee K and fitting parameter keys (appended 2026-10-09)

A tee may carry `params.k_model_run` and/or `params.k_model_branch`, each any §9.2
model object; they apply only to the straight-through (`run_ids[1]`) or branch
(`branch_run`) path respectively and only to tees (other types refuse with
"only tees have per-path K"). Tee precedence, highest first: the path's
`k_model_run`/`k_model_branch`; `k_model` (both paths); the Crane run/branch
correlation (attributed tees); the legacy tee constants. Method strings are
`k_model_run(<kind>)` / `k_model_branch(<kind>)`; existing `k_model(<kind>)` strings
are unchanged. `piping_solve`, pressure-drop reports and `run_loss_1d` share this.

`piping_fitting_k` accepts an optional top-level `path: "run"|"branch"` (tees only,
else `invalid_params`) selecting which tee path `K`/`method`/`curve` describe.

`piping_edit` `add_fitting`/`update_fitting` accept only the parameter keys of the
fitting's type — every type: `k_model`, `label`, `geometry`; elbow `angle`, `radius`,
`radius_mm`; tee `run_ids`, `branch_run`, `branch_nps`, `k_model_run`,
`k_model_branch`; reducer `nps2`; flange `flange_class`, `flange_face`; valve
`valve_type`; orifice `beta`, `bore_mm`. Any other key refuses with reason
`unknown_param`, the hint naming every offending key and the accepted set. A `null`
value (delete) and re-sending a value already stored are always accepted, so older
documents round-trip through the GUI unchanged.
