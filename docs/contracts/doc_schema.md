# Contract: Document Schema — `aicad.part/1` · `aicad.asm/1`

> Version 1. Changes require a schema-string bump and a migration note here.
> Machine-checked by `engine/doc/schema.py`; fixtures in `docs/contracts/fixtures/`.

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
  "features": [ Feature, … ]
}
```

- `material` may be `null`. `key` indexes `engine/sim/materials.json`.
- `features` array order **is** the history; the rollback bar is an index into it.
- `equations.globals` evaluate in dependency order; cycles are a document error.

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
| `spline` | `points:[[x,y],…]` (cubic through-points) |
| `slot` | `p1:[x,y]`, `p2:[x,y]`, `width:w` (straight slot, arc caps) |
| `text` (S) | `p:[x,y]`, `text:str`, `height:mm` |

All entities carry `construction:bool` (construction geometry never forms profile regions).

### 3.2 Handles

Constraint references use **handles**: `"e3"` = whole entity; point sub-refs:
`"e3.p1"`, `"e3.p2"` (line/slot endpoints), `"e3.center"` (circle/arc/ellipse),
`"e3.start"`, `"e3.end"` (arc endpoints), `"e3.p"` (point entity), `"e3.p0"…"e3.pN"` (spline pts).
Grammar: `^e\d+(\.(p1|p2|p|center|start|end|p\d+))?$`.

### 3.3 Constraints

```json
{"id": "c7", "type": "distance", "refs": ["e1.p1", "e1.p2"],
 "value": {"value": 80.0, "expr": "W"}, "driven": false}
```

| type | refs (arity, kinds) | value |
|---|---|---|
| `coincident` | 2 — point, point | — |
| `horizontal` | 1 line, or 2 points | — |
| `vertical` | 1 line, or 2 points | — |
| `parallel` | 2 — line, line | — |
| `perpendicular` | 2 — line, line | — |
| `tangent` | 2 — line/arc/circle pair (≥1 curved) | — |
| `equal` | 2 — same family (line-line length, arc/circle radius) | — |
| `concentric` | 2 — arc/circle, arc/circle | — |
| `midpoint` | 2 — point, line | — |
| `symmetric` | 3 — point/entity, point/entity, line (mirror axis) | — |
| `fix` | 1 — any handle | — |
| `point_on` | 2 — point, entity | — |
| `distance` | 2 — point/line, point/line | dimension value (mm) |
| `angle` | 2 — line, line | dimension value (deg) |
| `radius` | 1 — arc/circle | dimension value (mm) |
| `diameter` | 1 — circle | dimension value (mm) |

`driven:true` marks a reference dimension (measured, never enforced). Dimension constraints
(`distance|angle|radius|diameter`) MUST carry `value`; the rest MUST NOT.

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
