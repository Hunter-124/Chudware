# Contract: Document Schema — `aicad.part/1` · `aicad.asm/1` · `aicad.drawing/1`

> Version 1. Changes require a schema-string bump and a migration note here.
> Machine-checked by `engine/doc/schema.py`; fixtures in `contracts/fixtures/`.
>
> **W12 minor version bump (additive).** A NEW top-level document kind `aicad.drawing/1`
> (`DrawingDocument`, §8) is added — a 2D detailing document that projects a source part/assembly
> into orthographic + isometric views via HLR. This is a **new schema string**, NOT a change to the
> existing `aicad.part/1` / `aicad.asm/1` schemas (those are byte-for-byte unchanged and every
> existing part/assembly document remains valid). `validate_document` now dispatches a third schema
> string; readers that don't understand `aicad.drawing/1` reject it exactly like any unknown schema.
>
> **C35-overhaul migration (additive, no schema-string bump).** Three OPTIONAL fields are added;
> every pre-overhaul document remains valid (the fields default to absent/null), so the schema
> string stays `aicad.part/1`:
> - dimensional constraints may carry `placement:[u,v]` (annotation anchor, §3.3);
> - `distance` constraints may carry `axis:"h"|"v"` (§3.3);
> - the part/assembly document may carry `rollback_after: <feature id>|null` (the rollback bar, §1).
> A future change to any EXISTING field's meaning would still require the schema-string bump.
>
> **CONFIG migration (additive, no schema-string bump).** Two OPTIONAL top-level fields are added to
> `aicad.part/1` AND `aicad.asm/1` — `configurations` (a list) + `active_configuration` (a name); see
> §9. They drive named part/assembly variants by per-config feature-param overrides + feature
> (and, in assemblies, component) suppression. **ABSENT `configurations` is exactly today's
> behavior**: the document compiles as a single implicit `"Default"` configuration and is
> byte-identical to a pre-CONFIG rebuild — so every existing document remains valid and unchanged,
> and the schema string stays `aicad.part/1` / `aicad.asm/1`. This is the SECOND additive doc-schema
> version bump of the SolidWorks-class overhaul (the first being the C35-overhaul fields above).

## 0. Conventions

- All numbers are **canonical units: millimeters, degrees, grams** — regardless of the document's
  display unit system. `units.system` (`"MMGS"` | `"IPS"`) affects display + input parsing in the
  GUI only. This is normative for every producer and consumer.
- IDs are short opaque strings assigned by the worker, never reused within a document:
  features `"f1"…`, sketch entities `"e1"…`, sketch constraints `"c1"…`, components `"k1"…`,
  mates `"m1"…`. Document `id` is a UUID4 string.
- A **dimension value** is always the object `{"value": <float>, "expr": <string|null>}`.
  `expr != null` means expression-driven: re-evaluated each rebuild against the document equations
  (grammar in §4), and `value` is updated to the result. `expr == null` means `value` is authored.
- **Selectors** (references to faces/edges/vertices/bodies/regions) are defined in
  [selectors.md](selectors.md). Where a field accepts "selector-or-datum", a plain string is a
  feature id of a datum feature or a reserved built-in datum id (§5).

## 1. PartDocument

```json
{
  "schema": "aicad.part/1",
  "id": "6f0c…",
  "name": "bracket",
  "units": {"system": "MMGS"},
  "material": {"key": "alu_6061"},
  "equations": {"globals": [{"name": "thickness", "expr": "6"}]},
  "features": [ Feature, … ],
  "rollback_after": "f3"
}
```

- `material` may be `null`. `key` indexes `engine/sim/materials.json`.
- `features` array order **is** the history; the rollback bar is an index into it.
- `equations.globals` evaluate in dependency order; cycles are a document error.
- `rollback_after` (optional, C35-overhaul): the rollback bar. `null`/absent = bar at the end
  (compile everything). A feature id = the bar sits **after** that feature; features after it are not
  compiled and report `rolled_back` (worker_rpc). Must reference an existing feature id. The
  assembly document accepts the same field with the same meaning.

## 2. Feature envelope

```json
{
  "id": "f5",
  "type": "extrude",
  "name": "Boss-Extrude1",
  "suppressed": false,
  "params": { … },
  "refs": { … }
}
```

- `params` holds plain data (numbers, dimension values, strings, arrays).
- `refs` holds geometry/topology references (selectors, datum ids, feature ids, region selectors).
  Rule of thumb: anything that must survive re-resolution on rebuild goes in `refs`.
- Per-type `params`/`refs` fields are normative in [feature_params.md](feature_params.md).
- **v1 feature types**: `plane`, `axis`, `point`, `csys`, `sketch`, `box`, `cylinder`, `sphere`,
  `cone`, `torus`, `extrude`, `revolve`, `sweep`, `loft`, `fillet`, `chamfer`, `shell`, `draft`,
  `hole`, `linear_pattern`, `circular_pattern`, `mirror`, `boolean`, `helix`, `script`, `import`.
  Stretch (schema reserved, may not compile in v1): `rib`, `split`.
- `script` is the legacy escape hatch: `params.code` is a build123d script executed through the
  existing forgiving kernel namespace; its result/parts become this feature's bodies.

## 3. Sketch

A sketch is a feature (`type:"sketch"`) with `refs.plane` = selector-or-datum and
`params.entities` + `params.constraints`:

### 3.1 Entities

```json
{"id": "e1", "type": "line", "construction": false, "p1": [0,0], "p2": [80,0]}
```

| type | fields (sketch-plane coords, mm/deg) |
|---|---|
| `point` | `p:[x,y]` |
| `line` | `p1:[x,y]`, `p2:[x,y]` |
| `circle` | `center:[x,y]`, `radius:r` |
| `arc` | `center:[x,y]`, `radius:r`, `start:deg`, `end:deg`, `ccw:bool` (3-point arcs are normalized to this form on creation) |
| `ellipse` (S) | `center:[x,y]`, `rx`, `ry`, `rot:deg` |
| `ellipse_arc` (S) | `center:[x,y]`, `rx`, `ry`, `rot:deg`, `a0:deg`, `a1:deg` (partial ellipse; `a0`/`a1` are trim angles in the **un-rotated** ellipse frame, CCW `a0`→`a1`) |
| `parabola` (S) | `vertex:[x,y]`, `focus:[x,y]`, `t0`, `t1` (drawn segment of a parabola; own frame `u=unit(focus−vertex)`, `w=perp(u)`, `f=\|focus−vertex\|`, point `P(t)=vertex+(t²/4f)·u+t·w`; `t0`/`t1` are signed `w`-extents) |
| `conic` (S) | `p0:[x,y]`, `p1:[x,y]`, `apex:[x,y]`, `rho:0..1` (SolidWorks conic: endpoints + tangent-intersection `apex` + `rho`; rendered as rational quadratic Bézier with apex weight `w=rho/(1−rho)`) |
| `spline` | `points:[[x,y],…]` (cubic through-points) |
| `style_spline` (S) | `points:[[x,y],…]` (control **poles**: a clamped degree-`min(3,n−1)` B-spline by control polygon — interpolates only the first/last pole, tangent to the end legs, within the convex hull; distinct from `spline`, which interpolates through every point). Pole handles `p0…pN`. |
| `slot` | `p1:[x,y]`, `p2:[x,y]`, `width:w` (straight slot, arc caps) |
| `arc_slot` (S) | `center:[x,y]`, `radius:r`, `a0:deg`, `a1:deg`, `width:w` (arc-spine slot: spine = circular arc, center/radius, CCW `a0`→`a1` like `arc`; capsule of total width `w` around it. `radius − w/2 > 0` required) |
| `text` (S) | `p:[x,y]`, `text:str`, `height:mm` (`p` = lower-left; cap-height rescaled to `height`; build123d `Text` glyph faces, inner glyph holes preserved; optional `font:str?`. Solver: position-only (2 DOF)) |
| `eq_curve` (S) | `xt:str`, `yt:str`, `t1`, `t2` (SolidWorks Equation-Driven Curve: parametric `x(t)/y(t)` expression strings over `[t1,t2]` in radians, safe-AST evaluated, sampled to a spline; closed forms extrude as a region. Explicit `yx`/`x1`/`x2` form also compiles. Solver: 0 DOF — equation-fixed; `start`/`end` are constant references at `t1`/`t2`) |

All entities carry `construction:bool` (construction geometry never forms profile regions).

**Conic-family solver support (C28-A):** `ellipse_arc`/`parabola`/`conic` participate in the solve
with these DOF: ellipse_arc 7 (`cx,cy,rx,ry,rot,a0,a1`), parabola 6 (`vx,vy,fx,fy,t0,t1`),
conic 7 (`p0,p1,apex,rho`). Supported relations on these types: `coincident`
(on the handles below), `fix` (on any handle), `point_on` (a point handle lies on the
curve), and `tangent` against a **line** (C32-B). `point_on`/`tangent` are **range-free** —
they use the full conic (ellipse_arc ignores `a0`/`a1`, parabola ignores `t0`/`t1`), mirroring
how `arc`/`circle` `point_on` uses the full circle. `point_on` residual is the implicit
rotated-ellipse distance (ellipse_arc) or a closest-param projection offset (parabola/conic).
`tangent[line]` is exact for ellipse_arc (support-function residual) and numeric for
parabola/conic. NOT yet supported (deferred): curve↔curve / conic↔conic `tangent`,
`equal`, `concentric`, `radius`/`diameter`, `horizontal`/`vertical`,
`symmetric`, `midpoint`, `parallel`, `perpendicular`, `angle` for the conic family. b3d region
geometry: ellipse_arc → trimmed `Edge.make_ellipse`; parabola → cubic spline through sampled
points; conic → `Edge.make_bezier` with weights (sampled-spline fallback). All closed loops
containing these still build region faces.

**Arc-slot solver support (C30-B):** `arc_slot` participates in the solve with 6 DOF
(`cx,cy,r,a0,a1,w`). Spine-endpoint handles `start`/`end` carry analytic partials (like `arc`).
Supported relations: `coincident` (on `center`/`start`/`end`) and `fix` (on any handle); all
others are skipped-silently (same convention as the conic family). b3d region geometry: a closed
capsule wire (outer arc `r+w/2`, end-cap half-circle `w/2`, inner arc `r−w/2` reversed, start cap);
`radius − w/2 ≤ 0` raises a FeatureError.

### 3.2 Handles

Constraint references use **handles**: `"e3"` = whole entity; point sub-refs:
`"e3.p1"`, `"e3.p2"` (line/slot endpoints), `"e3.center"` (circle/arc/ellipse/ellipse_arc),
`"e3.start"`, `"e3.end"` (arc/ellipse_arc/parabola/conic/arc_slot endpoints; for `arc_slot` these are the derived spine endpoints), `"e3.p"` (point entity),
`"e3.p0"…"e3.pN"` (spline pts); conic-family extras: `"e3.vertex"` (parabola),
`"e3.apex"` (conic). Grammar: `^e\d+(\.(p1|p2|p|center|start|end|vertex|apex|p\d+))?$`.

### 3.3 Constraints

```json
{"id": "c7", "type": "distance", "refs": ["e1.p1", "e1.p2"],
 "value": {"value": 80.0, "expr": "W"}, "driven": false,
 "axis": null, "placement": [40.0, -5.0]}
```

| type | refs (arity, kinds) | value |
|---|---|---|
| `coincident` | 2 — point, point | — |
| `horizontal` | 1 line, or 2 points | — |
| `vertical` | 1 line, or 2 points | — |
| `parallel` | 2 — line, line | — |
| `perpendicular` | 2 — line, line | — |
| `tangent` | 2 — line/arc/circle pair (≥1 curved) | — |
| `collinear` | 2 — line, line | — |
| `equal` | 2 — same family (line-line length, arc/circle radius incl. mixed arc↔circle) | — |
| `concentric` | 2 — arc/circle, arc/circle | — |
| `coradial` | 2 — arc/circle, arc/circle (same center **and** radius) | — |
| `midpoint` | 2 — point, line | — |
| `symmetric` | 3 — point/entity, point/entity, line (mirror axis) | — |
| `fix` | 1 — any handle | — |
| `point_on` | 2 — point, entity | — |
| `distance` | 2 — `[point, point]` or `[point, line entity]` | dimension value (mm) |
| `angle` | 2 — line entity, line entity | dimension value (deg), measured/reported in `[0,180)` |
| `radius` | 1 — arc/circle | dimension value (mm) |
| `diameter` | 1 — circle | dimension value (mm) |

`driven:true` marks a reference dimension (measured, never enforced). Dimension constraints
(`distance|angle|radius|diameter`) MUST carry `value`; the rest MUST NOT.

**Tangent branch (curve↔curve):** a `tangent` between two arcs/circles admits two arrangements —
*external* (`|C0−C1| = r0+r1`) and *internal* (`|C0−C1| = |r0−r1|`, one nested inside the other).
The solver picks the branch matching the **as-drawn** geometry (whichever residual is smaller at
constraint-build time) and freezes it for the solve, so a nested pair is never flipped inside-out.
The line↔arc/circle tangent (distance from center to line = radius) is unaffected.

**collinear vs parallel:** `collinear` forces both lines onto one infinite line (parallel directions
+ one endpoint on the other line, 2 DOF removed); `parallel` only equates directions (1 DOF).
`coradial` = `concentric` + equal radius rolled into one relation (3 DOF removed).

**Optional dimensional fields (C35-overhaul):**
- `placement:[u,v]` — sketch-plane coordinates of the annotation text anchor (where the sketcher
  draws the dimension label). Optional, `null`/absent if unplaced; round-trips through save/open.
- `distance.axis:"h"|"v"` — constrain only the horizontal (`|Δu|`) or vertical (`|Δv|`) gap;
  `null`/absent = aligned Euclidean distance. **Point-point refs only** — a `[point, line]` distance
  (perpendicular foot distance to the infinite line) is already directional and rejects `axis`.

### 3.4 Solver status (computed, not stored)

The solver reports per-entity status (`under` | `full` | `over`) and sketch DOF; the document
never persists solve state. Entity coordinates stored in the document are the **last solved**
positions (so documents open without solving).

## 4. Equations — expression grammar

```
expr   := number | name | expr op expr | -expr | func "(" expr {"," expr} ")" | "(" expr ")"
op     := + | - | * | / | ^        (^ is power; ** also accepted)
func   := sin cos tan asin acos atan sqrt abs min max floor ceil
const  := pi
```

- **Trig is in DEGREES** (in and out: `sin(30) = 0.5`, `asin(0.5) = 30`). CAD-user convention.
- Names resolve against `equations.globals` (v1 scope). Unknown name, cycle, division by zero,
  or non-finite result = evaluation error → the owning feature gets an error state.
- Implemented by `engine/doc/equations.py` (whitelisted-AST evaluator; nothing else executes).

## 5. Reserved built-in datum ids

Every document implicitly contains: `origin`, `plane.front` (XZ… SolidWorks Front = XY in our
Z-up world; normative mapping below), `plane.top`, `plane.right`, `axis.x`, `axis.y`, `axis.z`.

Normative geometry (Z-up, matching the existing engine/render conventions):

| id | definition |
|---|---|
| `plane.top` | Z=0 plane, normal +Z |
| `plane.front` | Y=0 plane, normal −Y |
| `plane.right` | X=0 plane, normal +X |
| `origin` | (0,0,0) |
| `axis.x/y/z` | through origin |

## 6. AssemblyDocument

```json
{
  "schema": "aicad.asm/1",
  "id": "…", "name": "clamp", "units": {"system": "MMGS"},
  "components": [
    {"id": "k1", "name": "base", "source": {"path": "base.step+"},
     "transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1],
     "fixed": true, "suppressed": false}
  ],
  "mates": [
    {"id": "m1", "type": "coincident", "align": "aligned", "suppressed": false,
     "refs": [{"component": "k1", "selector": { … }},
              {"component": "k2", "selector": { … }}],
     "value": null}
  ],
  "features": [ Feature, … ],
  "explode": [{"component": "k2", "vector": [0,0,1], "distance": 40.0}]
}
```

- `transform` is 16 floats, **row-major**, rotation+translation only (no scale/shear).
- `source.path` is relative to the assembly file's directory when possible; `.step+` preferred,
  STEP accepted (frozen geometry). `source.embedded` (string doc id) reserved for embedded parts
  inside a `.step+` container.
- Mate `type` ∈ `coincident | parallel | perpendicular | tangent | concentric | distance | angle
  | lock`. `distance`/`angle` carry a dimension `value`; others `value:null`.
  `align` ∈ `aligned | anti`.
- `features` on assemblies: `linear_pattern`/`circular_pattern` of components (see
  feature_params.md `asm_*_pattern`), reserved for future assembly features.
- First inserted component defaults `fixed:true`.

## 7. Versioning & compatibility

- Readers MUST reject an unknown major schema string (`aicad.part/2`).
- Readers MUST ignore unknown keys (forward-tolerant) and preserve them on resave when feasible.
- Writers MUST emit every required key listed here.

## 8. DrawingDocument (`aicad.drawing/1`, W12)

A **drawing** is a 2D detailing document that projects a SOURCE part/assembly into orthographic +
isometric views via hidden-line removal (HLR). It carries no features/equations/material — only a
`source` reference and sheets of views. The projected 2D geometry is **not stored** in the document
(it is recomputed from the source's current bodies on every `drawing_get`, so the drawing stays
associative); the document only stores the view *definitions*.

```json
{
  "schema": "aicad.drawing/1",
  "id": "doc-…",
  "name": "Bracket - Drawing",
  "units": {"system": "MMGS"},
  "source": "<source part/assembly doc id>",
  "sheets": [
    {
      "id": "s1",
      "size": "A3",
      "views": [
        {"id": "v1", "type": "base", "orientation": "front",
         "scale": 1.0, "position": [100, 100], "show_hidden": true},
        {"id": "v2", "type": "projected", "orientation": "top",
         "scale": 1.0, "position": [100, 200], "show_hidden": true},
        {"id": "v3", "type": "iso", "orientation": "iso",
         "scale": 1.0, "position": [250, 200], "show_hidden": false}
      ]
    }
  ]
}
```

- `source` — the source doc to project. A **string** is an open source doc id (the live-session
  form W12 uses) or a path; the **object** form `{path}` / `{doc_id}` is reserved for embedded
  sources. The source must resolve to a part or assembly with compiled bodies at projection time.
- `sheets` — non-empty list. Each sheet: `id` (`s<N>`), `size` (∈ `A4 A3 A2 A1 A0` ISO + `A B C D E`
  ANSI), `views`. View `id`s (`v<N>`) are **document-unique** (not just per-sheet).
- **View** = `{id, type, orientation, scale, position:[x,y], show_hidden}`:
  - `type` ∈ `base` | `projected` | `iso`. `base` = the primary orthographic view; `projected` =
    an ortho view folded off the base (front/top/right/…); `iso` = the standard isometric.
  - `orientation` ∈ `front back top bottom left right iso`. An `iso` view MUST be orientation
    `iso`; a `base`/`projected` view MUST NOT be `iso`.
  - `scale` — positive float (drawn size ÷ true size). `position:[x,y]` — sheet-mm anchor of the
    view's projected-origin. `show_hidden` — bool, draw hidden (dashed) edges.
  - **Reserved (W13):** a view may carry `annotations:[…]` (list) and `label:str|null`.

**Orientation → projection direction (Z-up, matches §5 reserved datums).** The orientation is the
HLR line of sight; `front` looks along **+Y** (the viewer sits at −Y, exactly like `plane.front`'s
−Y normal):

| orientation | view dir (eye→model) | sheet +x | sheet +y |
|---|---|---|---|
| `front` | +Y | +X | +Z |
| `back` | −Y | −X | +Z |
| `top` | −Z | +X | +Y |
| `bottom` | +Z | +X | −Y |
| `right` | −X | +Y | +Z |
| `left` | +X | −Y | +Z |
| `iso` | −(1,1,1) (eye at (1,1,1)) | iso horizontal | world +Z up |

Canonical units mm. A projection failure on one view is **contained**: that view comes back with
`error:"<reason>"` and empty geometry; it never invalidates the other views or the document.

The 2D view-geometry JSON shape (what `drawing_get` / `drawing_add_view` return per view) is
normative in [worker_rpc.md §Drawings](worker_rpc.md). Implemented + machine-checked by
`engine/doc/drawing.py` + `engine/doc/schema.py::validate_drawing`.

## 9. Configurations (CONFIG — additive)

A part (`aicad.part/1`) or assembly (`aicad.asm/1`) document MAY carry named **configurations** —
variants of the one document driven by per-config feature-param overrides + feature (and, in
assemblies, component) suppression. This is the SolidWorks "Configurations" feature.

```json
{
  "schema": "aicad.part/1",
  "…": "…",
  "features": [ … ],
  "configurations": [
    {"name": "Default", "param_overrides": {}, "suppressions": {}},
    {"name": "Small",
     "param_overrides": {"f2.depth": {"value": 20, "expr": null}},
     "suppressions": {"f3": true}},
    {"name": "Large",
     "param_overrides": {"f2.depth": {"value": 40, "expr": null}},
     "suppressions": {}}
  ],
  "active_configuration": "Small"
}
```

- `configurations` — OPTIONAL list. Each entry:
  - `name` — non-empty, **unique** within the document. The display + lookup key.
  - `param_overrides` — object keyed `"<feature_id>.<param_key>"`; the value REPLACES that feature's
    stored `params[<param_key>]` when the config is active. The value is whatever the param holds —
    a scalar (`int`/`bool`/`str`), an array, or a **dimension value** `{value, expr}` (dim-shaped
    overrides are validated like any stored dim). The key's `<feature_id>` must exist and (for the
    structural check) `<param_key>` must be a key of that feature's `params`.
  - `suppressions` — object keyed by `<feature_id>` (and, in assemblies, `<component_id>` `k<N>`) →
    `bool`. A `true` entry suppresses that feature/component **in addition to** its base
    `suppressed` flag (the effective flag is `base.suppressed OR config.suppressions[id]`). A
    `false`/absent entry does nothing. The id must exist.
- `active_configuration` — OPTIONAL name of the active config. When `configurations` is present but
  this is absent/unknown, the **first** configuration is active. When `configurations` is absent it
  has no meaning (and validates only as a string-or-null).

**Absent = implicit Default (byte-identical).** A document with no `configurations` (or an empty
list) compiles as a single implicit configuration named `"Default"` with no overrides and no extra
suppression — i.e. exactly the pre-CONFIG behavior. Such a document is byte-for-byte valid and
compiles to identical geometry, status, and topology as before this version. The compiler echoes the
active config name on its result (`"Default"` for config-less docs).

**Compile semantics.** Activating a config recompiles the model with the active config's overrides +
suppression applied to a **non-mutating overlay** of the stored feature list — the stored document is
never edited. Everything else (equation resolution, per-feature caching, owner-tagging, tessellation)
flows unchanged; the compile cache keys on the active config so switching configs recomputes only
what changed and caches per-config (switching back is a cache hit). A config that fails to compile
reports per-feature errors like any normal rebuild — it never crashes.

**Persistence.** `configurations` + `active_configuration` are part of `document.json` and round-trip
through `.step+` save/open. Reopening recompiles the active configuration.

Implemented + machine-checked by `engine/doc/schema.py::_validate_configurations`,
`engine/doc/compiler.py` (`resolve_active_config` / `apply_configuration_to_doc` /
`_config_effective_features`), and `engine/doc/test_configurations.py`. The `config_*` worker RPCs
that drive configurations live in [worker_rpc.md §Configurations](worker_rpc.md).
