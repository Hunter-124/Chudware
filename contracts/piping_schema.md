# Contract: `aicad.piping/1` document schema

> **Frozen seam.** Append-only; changes require integrator sign-off + a version bump
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
