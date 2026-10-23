# Contract: Document Schema — `aicad.part/1` · `aicad.asm/1` · `aicad.drawing/1`

> Version 1. Changes require a schema-string bump and a migration note here.
> Machine-checked by `core/doc/validate.cpp`; fixtures in `contracts/fixtures/`.
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

- `material` may be `null`. `key` indexes `services/sim/materials.json`.
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
- **v1 feature types**: `plane`, `axis`, `point`, `csys`, `sketch`, `sketch3d`, `box`,
  `cylinder`, `sphere`, `cone`, `torus`, `extrude`, `revolve`, `sweep`, `loft`, `fillet`,
  `chamfer`, `shell`, `draft`, `hole`, `linear_pattern`, `circular_pattern`, `mirror`,
  `boolean`, `helix`, `script`, `import`.
  Stretch (schema reserved, may not compile in v1): `rib`, `split`.
  (`sketch3d` is a SKETCH3D-migration additive type — see §3B; every pre-existing document
  remains valid, schema string unchanged.)
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

### 3.5 External references (associative Convert Entities — 2026-07-07)

A sketch may additionally carry `params.external_refs: [ExternalRef…]` — the persistent record
behind associative `sketch_convert` (doc_ops.md):

```json
{"id": "x1",
 "source": {"kind": "face", "selector": { ...selectors.md selector... }},
 "construction": false, "fix": true,
 "entity_ids": ["e5", "e6", "e7", "e8"],
 "stale": false}
```

| field | meaning |
|---|---|
| `id` | `x<N>` (its own id family; minted by the op id generator) |
| `source` | the projected Ref: `{datum:"front"|"top"|"right"}` or `{kind:"face"|"edge", selector}` |
| `construction` / `fix` | the creation-time flags — re-projection re-applies them to grown entities |
| `entity_ids` | the sketch entities this ref OWNS, in projection order (ids are STABLE across re-projection) |
| `stale` | `true` when the last rebuild could not re-resolve `source` (strict selectors.md §3 match); last-good entities kept, sketch compiles with a warning |

Semantics (normative): on every mutating rebuild the engine re-resolves `source` against the
compiled model and re-projects onto the sketch plane, patching the owned entities in place.
Constraints/dims attached to owned entities survive geometry updates; an entity whose projected
TYPE changes has its touching constraints cascade-dropped (+ `fix` re-pinned). Owned entities are
solver-FIXED via ordinary `fix` constraints — users constrain other geometry TO them but cannot
drag them. `sketch_break_external_ref` severs the link. Converted geometry defaults to REAL
(non-construction) entities: closed converted loops are extrudable profiles.

## 3B. 3D Sketch (`sketch3d`)

> **SKETCH3D migration (additive, no schema-string bump).** A NEW feature type `sketch3d` —
> a FREE-SPACE 3D sketch (no plane): points, lines, arcs, splines in world coordinates with a
> core 3D constraint set. It drives sweep paths, routing runs, and weldment skeletons. Every
> pre-existing document remains valid. Full GUI/RPC contract (entity/constraint JSON, health/
> drag RPC examples, wire referencing): [sketch3d_gui_contract.md](sketch3d_gui_contract.md).

A `sketch3d` is a feature (`type:"sketch3d"`) with **no refs** and
`params.entities` + `params.constraints`. All coordinates are **world millimeters**.

### 3B.1 Entities

```json
{"id": "e1", "type": "line", "construction": false, "p1": [0,0,0], "p2": [0,0,80]}
```

| type | fields (world coords, mm) |
|---|---|
| `point` | `p:[x,y,z]` |
| `line` | `p1:[x,y,z]`, `p2:[x,y,z]` |
| `arc` | `p1:[x,y,z]`, `pm:[x,y,z]`, `p2:[x,y,z]` — **THREE-POINT** arc: start / interior "through" point / end. (Chosen over center+normal+radius+angles: uniform point parameterization — no unit-normal normalization in the solve, natural endpoint handles for chaining, 1:1 GUI point drags. Collinear `p1`/`pm`/`p2` is degenerate and fails edge compile.) |
| `spline` | `points:[[x,y,z],…]` (≥ 2; cubic interpolation through every point, like the 2D `spline`) |
| `eq_curve3d` | `xt:str`, `yt:str`, `zt:str`, `t1`, `t2` — parametric `x(t)/y(t)/z(t)` expression strings over `t∈[t1,t2]`, evaluated against the document equation grammar (§4 — degrees trig, globals; `t` is bound per-sample, not a global name). Compiled by sampling 33 points and interpolating a cubic BSpline through them (mirrors the 2D `eq_curve`'s sample-to-spline compile). **Solver: 0 DOF — equation-fixed** (rigid, like the 2D `eq_curve`): `p1`/`p2` (the endpoints at `t1`/`t2`) are pinned by an implicit fix residual right after allocation, so they are usable as `coincident`/`distance`/`point_on` targets for chaining but never move under any other constraint. Use for helices, sinusoidal routing runs, etc. — e.g. a helix: `xt:"R*cos(t)"`, `yt:"R*sin(t)"`, `zt:"P*t/360"`, `t1:0`, `t2:"720"` (two turns, `P`=pitch/turn, degrees). |

All entities carry `construction:bool` (construction geometry never joins wire chains).

### 3B.2 Handles

`"e3"` = whole entity; `"e3.p"` (point); `"e3.p1"` / `"e3.p2"` (line + arc endpoints; `eq_curve3d`
also uses `p1`/`p2` for its `t1`/`t2` endpoints; aliases `"e3.start"`/`"e3.end"` accepted);
`"e3.pm"` (arc through-point; alias `"e3.mid"`); `"e3.p0"…"e3.pN"` (spline points). Grammar:
`^e\d+(\.(p|p1|p2|pm|start|end|mid|p\d+))?$`.

### 3B.1b External model references (v1)

A `sketch3d` feature may additionally carry `external_refs: [ExternalRef3…]` — persistent
records that ground a constraint against a **vertex on the built model** (use case: "a
spline that interacts with a specific corner on my part"), mirroring the 2D §3.5 external-ref
semantics but at the doc-feature level (not nested under a sketch plane).

```json
{"id": "x1", "source": {"kind": "vertex", "selector": {...}}, "stale": false,
 "last_point": [12.5, 40.0, 0.0]}
```

| field | meaning |
|---|---|
| `id` | mints an `x<N>` id (same id-space convention as 2D external_refs), usable directly as a POINT handle in any constraint's `refs` (e.g. `coincident: ["e5.p2", "x1"]`, `distance: ["e3.p1", "x1"]`) — no `.p` suffix, `x1` alone is the point. |
| `source.kind` | **`"vertex"` is the v1-supported, REQUIRED-to-work kind** — resolves to a fixed 3D point. `"edge"` / `"face"` are accepted in the schema (forward-compat) but are **NOT resolved in v1** — they are always left `stale:true` (an honest gate, not a silent no-op; see below). |
| `source.selector` | a normal geometric selector (contracts/selectors.md — kind/surf/centroid/axis/measure/owner_feature/hint_index), built the same way `query_geometry`/pick selectors are built elsewhere in the doc. For a vertex this is the `describe_vertices` fingerprint. |
| `stale` | `true` when the selector failed to re-resolve on the last rebuild (owner feature deleted, geometry moved past the score margin) **or** `source.kind` isn't `"vertex"`. The ref's `last_point` (if any) is kept — never wiped — so the sketch keeps solving at its last-good ground position, same "never block the edit" contract as 2D. |
| `last_point` | the resolved `[x,y,z]` world point, written by the resolver on every successful re-resolve. Absent until the first successful resolve (a ref that has NEVER resolved is not injected into the solve at all — any constraint referencing it forward-tolerantly fails to resolve, like any unknown handle). |

**Re-resolution + solve ordering (every mutating rebuild):** `refresh_sketch3d_external_refs`
(core/doc/session.cpp) runs BEFORE `compile_document`, re-resolving every sketch3d's vertex
selectors against the document's compiled body/bodies and writing back `last_point`/`stale`.
`compile_document`'s `sketch3d` branch then injects each ref with a `last_point` as a fixed
`construction:true` point entity (id = the ref id) plus an implicit `fix` constraint, and solves
normally — so the constrained geometry (e.g. a spline endpoint pinned `coincident` to `x1`)
follows the source vertex on every rebuild. A `sketch3d_health` call surfaces stale refs as a
feature-status **warning** (never a failure), same shape as the 2D external-ref reporting.
**Downstream ordering:** a `sweep` (or anything else) consuming this `sketch3d`'s wire compiles
strictly after it in feature order, so it always sees the already-re-solved geometry — no
separate re-run needed beyond the normal feature-tree compile order.

**v1 simplification (documented gap):** refs resolve against the document's FINAL compiled
body/bodies (same convention `refresh_sketch_external_refs` uses for 2D face/edge refs), not
strictly "the body as of this sketch3d's position in history." In practice this means a
sketch3d authored early can reference a vertex that only exists after a LATER feature — usually
the more useful behavior ("chase the final corner"), but it is not a strict topological-order
guarantee and isn't specially guarded beyond the existing rebuild recursion protections.
**GUI creation flow (for the agent wiring the picker):** the host has the picked vertex's owner
feature id + world position + a topology descriptor from its id-pass (the same pipeline that
backs `doc_resolve_pick`); it should call the new MCP tool `sketch3d_ref_vertex(sketch_id,
query)` (or the underlying `doc_apply_ops` `add_sketch3d_external_ref` op — see
sketch3d_gui_contract.md) with either that pick descriptor or a `query_geometry`-style
plain-word query; the engine mints the `x<N>` id and returns it for the GUI to use as a handle
in the next `add_constraints` call.

### 3B.3 Constraints (v1)

| type | refs (arity, kinds) | value / extra fields |
|---|---|---|
| `coincident` | 2 — point handle, point handle | — |
| `fix` | 1 — any handle or whole entity (pins at authored coords) | — |
| `distance` | 2 — `[point, point]` or `[point, <bare line entity id>]` (perpendicular gap to the **infinite** line) | dimension value (mm) |
| `length` | 1 — line entity | dimension value (mm) |
| `parallel` | 2 — line, line | — |
| `perpendicular` | 2 — line, line | — |
| `angle` | 2 — line, line | dimension value (deg, `[0,180]`) |
| `along_axis` | 1 — line | `axis:"x"\|"y"\|"z"` **or** `dir:[x,y,z]` (constraint-record field, not `value`) |
| `point_on` | 2 — point, line **or** arc entity (arc = full circumcircle, range-free like 2D) | — |

Dimension constraints (`distance|length|angle`) MUST carry `value` (`{value, expr}` — expr
evaluated against document equations, same as §3.3); the rest MUST NOT. `driven:true` marks a
reference dimension (measured, never enforced). **Deferred to v2:** `point_on` a spline,
tangent, symmetric, equal, midpoint.

### 3B.4 Solve, statuses, wires (computed, not stored)

Same solver-status shape as §3.4: per-entity `under|full|over`, sketch `dof`,
`redundant`/`conflicting` constraint ids. Stored coordinates are the last solved/committed
positions. The compiler chains non-construction line/arc/spline entities into **3D wire
chains** by endpoint proximity (tol 1e-4, mixed-entity float gaps stitched); chain order is
deterministic (wire 0 = the chain containing the earliest-authored entity). A downstream
`sweep` references a chain as its `path` or a `guides[]` entry: a bare feature id when the
sketch has exactly one chain, else `{"feature":"<fid>","wire":<k>}`.

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
- Implemented by `core/util/expr.cpp` (whitelisted-AST evaluator; nothing else executes).

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
`core/doc/drawing.cpp` + `core/doc/validate.cpp::validate_drawing`.

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

Implemented + machine-checked by `core/doc/validate.cpp::_validate_configurations`,
`core/doc/document.cpp` (`resolve_active_config` / `apply_configuration_to_doc` /
`_config_effective_features`). The `config_*` worker RPCs
that drive configurations live in [worker_rpc.md §Configurations](worker_rpc.md).

## Appendix A — Canonical `document.json` dump policy

**Goal:** serializing the same document tree repeatedly must yield byte-identical
`document.json` without changing its numeric values. PLM history snapshots, correction
traces (AI telemetry), and `doc_diff` must retain even small changes to axes and transforms.

### Rules (normative for every `document.json` producer)

| Rule | Detail |
|---|---|
| **Sorted keys** | Object keys MUST be emitted in lexicographic (ASCII) order. |
| **Numeric precision** | Every number MUST retain its full round-trip precision. Serialization, history snapshots, live synchronization, and semantic diffing MUST NOT quantize floats. Decimal rounding of dimensionless axes or rotation coefficients can make otherwise consistent constraints unsolvable after reopening. |
| **No timestamps** | `document.json` MUST NOT contain wall-clock timestamps or build counters. Timestamps belong in `meta.json` only (exempt from the byte-stability requirement). |
| **indent=2** | Two-space indentation, no trailing whitespace. |
| **UTF-8, no BOM** | The file is UTF-8 encoded with no byte-order mark. |

### Noise sources eliminated (from §3.2 checklist)

| Source | Fix |
|---|---|
| Non-canonical key order in some emitters | Sort keys |
| Timestamps inside `document.json` | Moved to `meta.json` exclusively |

Numerical differences are document data, not serialization noise. Display formatting
may shorten numbers without changing stored values. Older six-decimal files remain
readable, but discarded precision cannot be recovered from them; restore original
authored declarations or regenerate from preserved sources rather than relax solver
tolerances.

### Implementations

- **C++** (`.step+` save path): `core/doc/stepplus.cpp::canonical_doc_json()` — calls
  `doc.dump(2)`, omitting only an explicitly excluded root entry when requested.
  `nlohmann::json` uses `std::map` → keys are already sorted.
- **Python** (utility + ECAD already canonical): `services/plm/canonical.py::canonical_dump()` —
  calls `json.dumps(doc, indent=2, sort_keys=True)` without changing numbers.
- ECAD `.ecad+` save (`services/formats/formats.py`) likewise preserves numbers and
  uses sorted keys.

### `doc_diff` — semantic document diff

`doc_diff(rev_a, rev_b)` computes a feature-level delta between two canonical `document.json`
dicts. Use `canonical_dump` before persisting and parsed dictionaries for in-memory
diffs. Numeric changes are compared without quantization. The diff is keyed on stable ids (`f*`, `e*`, `c*`,
`k*`, `m*`) and returns:

```json
{
  "features_added":   ["f4", "f5"],
  "features_removed": ["f2"],
  "features_changed": ["f3"],
  "params_changed": [
    {"feature": "f3", "param": "depth", "old": {"value": 10.0, "expr": null},
                                         "new": {"value": 15.0, "expr": null}}
  ],
  "sketch_deltas": [
    {"feature": "f1", "entities_added": [], "entities_removed": ["e7"],
     "constraints_added": [], "constraints_removed": []}
  ],
  "mate_deltas": [],
  "suppression_flips": [{"id": "f2", "old": false, "new": true}]
}
```

`refs` churn is ignored when the resolved target is unchanged (ref id re-numbering after
paste/undo must not produce false positives).

Implemented in `services/mcp/doc_diff_tools.py`, registered as MCP tool `doc_diff`.

## Assembly angle hinge-line extension (2026-09-08)

An assembly `angle` mate accepts two or three references. The first two select
the measured directions; the optional third selects axial geometry carrying the
hinge line's point and direction. The existing `value` dimension object may also
carry `axis` (alias `reference_axis`) and `axis_point`, in the first reference
component's local frame. See `doc_ops.md` §10 and `worker_rpc.md` §29 for motion
drive resolution, composition, refusal and pose evidence semantics. This does
not change sketch angle constraint arities or make a scalar assembly angle mate
itself a five-constraint revolute joint.

## Solid feature modes (2026-09-10)

This is the authoritative body policy for `box`, `cylinder`, `sphere`, `cone`,
`torus`, `extrude`, `revolve`, `sweep`, and `loft`, including features submitted
through `add_features`. Options live in `params`; the feature-op API also accepts
them at feature level and hoists them into `params`.

| Mode | Body semantics |
|---|---|
| `new` | Create an independent body. Never boolean with existing bodies, even when fully contained, interpenetrating, coincident, or touching. Existing bodies and their volumes remain unchanged. |
| `add` | Fuse the generated solid with **all and only** surviving bodies it touches or overlaps, not simply the most recently created body. Consume those targets; leave every other body unchanged. Without contact, create an independent body. |
| `boss` | Same semantics as `add`. |
| `cut` | Subtract the generated tool from the most recent surviving body. Refuse when there is no target or no material is removed. Other bodies remain unchanged. |

`merge:false` overrides any valid mode to independent-body creation (`new`);
`merge:true` never overrides an explicit `new`. Unknown modes are rejected by
name, including when `merge:false` is present; they never silently become `add`.
Contact uses exact B-rep distance at OCCT's modeling precision (1e-7 mm), with
bounding boxes only as an early rejection. A positive 0.050 mm clearance is not
contact and must not be bridged by an additive boolean.

Omitted mode retains the existing compiler defaults: `boss` for primitives,
`revolve`, and `sweep`; `new` for flat `extrude` and `loft` inputs. Canonical
profile-feature schemas require an explicit `params.mode`; callers should always
state their intended body policy. The `op` of an explicit `boolean` feature and
the engraving/thread-specific `mode` vocabularies are separate contracts.

## Persistent assembly mate reference frames (2026-09-11)

Native mates carry engine-owned `reference_frames`: an object keyed by referenced
component ID, with each value a 16-number row-major component placement. New mates
capture these frames when created; caller-supplied bindings in `asm_add_mate` are
discarded. Legacy mates without bindings capture the first observed placements
before compilation or operation-batch placement changes.

These are the stable frames for derived angle datums, virtual hinge pins and lock
rest transforms, not live component poses. Solved-transform writeback, scalar or
alignment edits, suppression, rebuild and save/reopen do not redefine them.
Serialization follows the existing canonical document precision policy. Motion
binds any missing legacy frames only in its private snapshot; it does not mutate
the source document. A legacy file lacking this metadata cannot establish a lost
original zero frame; first-observed binding preserves its existing interpretation,
not an inferred design datum.

## Sketch `fix` added by op (append-only, 2026-10-10)

A `fix` constraint pins the entity at its authored coordinates. When `sketch_add_constraint`
adds a `fix`, the op first moves the authored coordinates of every entity to the solved
geometry (only if the sketch converges). Thus the fix locks the shape that the readback shows.
A `fix` that comes in through `add_feature` or `edit_feature` keeps the authored coordinates.

## Fillet on an edge shared by two bodies (append-only, 2026-10-12)

Bodies that meet only along a line (zero clearance under the contact rule of "Solid feature
modes") share that edge; an `add`/`boss` keeps them in one result as two solids. A `fillet`
whose explicit `edges` include such an edge fills every concave corner around it (each corner
under 180°) with rolling-ball fillet material of the feature's radius, and fuses that material
in, so the two solids become one solid. Ordinary edges in the same feature round as usual.
The shared edge must be straight or circular, and its four faces must run straight away from
it (planes, cylinders, cones). Otherwise the feature fails with that reason. It also fails by
name when the radius does not fit those faces. A positive clearance is not a shared edge;
such bodies stay separate.

## Sketch relation set v2, virtual axes and external reference kinds (append-only, 2026-10-16)

Extends §3.2/§3.3/§3.5. Earlier rows stay valid; where they disagree, this section wins.

**Virtual handles.** Besides `origin` (fixed point at the sketch origin), `axis.h` and `axis.v` are
fixed lines through the origin along the sketch +u / +v axes. They carry no variables and are
valid wherever a line ref is (parallel, perpendicular, collinear, angle, symmetric axis,
point_on, distance).

**Relations** (`refs` arity and kinds; the validator rejects other entity types by name):

| type | refs | meaning |
|---|---|---|
| `intersection` | point, curve, curve | the point lies on both curves (line infinite; circle/arc/ellipse/ellipse_arc/parabola/conic/spline full curve) |
| `midpoint` | point, line \| arc | arc: the mid-sweep point |
| `concentric` | 2 × circle/arc/ellipse/ellipse_arc/arc_slot or point handle | a point handle sits at the other's center; lines are rejected |
| `equal` | same family | adds ellipse/ellipse_arc (rx and ry), slot (length and width), arc_slot (radius and width) |
| `equal_curve_length` | line \| arc, line \| arc | equal curve length (arc length = r·\|sweep\|) |
| `tangent` | 2 curves | adds full `ellipse`; a spline/style_spline endpoint handle (`start`/`end`/first-last `pK`) or an arc `start`/`end` gives endpoint (G1) tangency |
| `smooth` | spline endpoint handle, line/circle/arc or spline endpoint handle | G1 + equal curvature (G2) at that end; does not add the coincidence |
| `point_on` | point, entity | adds spline/style_spline |
| `symmetric` | A, B, axis | A/B two point handles, two lines (endpoints mirrored) or two circles/arcs (centers mirrored + equal radius); axis = line or `axis.h`/`axis.v` |
| `parallel`, `perpendicular`, `collinear`, `angle` | lines | line, slot spine, `axis.h`/`axis.v` |
| `distance` | `[line, line]` | perpendicular distance from the second line's midpoint to the first (parallel-line dimension) |
| `arc_length` | 1 arc | dimension: r·\|sweep\| = value (mm) |

`collinear` is two normalized endpoint-to-line distances against the reference line (a fixed,
external or virtual line when one side has one), so a free line never satisfies it by
collapsing; `parallel`/`perpendicular` use angular residuals for the same reason.

A `tangent` between two curves whose endpoints are joined by `coincident` (directly or through
other coincident points) uses the junction (endpoint-direction) form; only unjoined curves use
the distance form. A classic slot (2 lines + 2 arcs, joined, tangent) therefore reports DOF 0
when dimensioned.

**Skipped constraints.** Solve results carry `skipped_constraints: [{id, type, reason}]` for every
stored constraint the solver could not apply (it never aborts the solve and never applies a
wrong-type residual).

**`params.auto_define`** (optional bool on a sketch feature) records an agent/user opt-out of
the automatic `sketch_fully_define` the MCP `add_features` path appends (doc_ops §31).

**External reference kinds** (`params.external_refs[].source`, `sketch_convert` refs,
`sketch_project_ref`/`sketch_project_entities`):

| source | projects to |
|---|---|
| `{datum:"front"\|"top"\|"right"}` | intersection line with the sketch plane (unchanged) |
| `{datum:"axis.x"\|"axis.y"\|"axis.z"}` | the world axis projected along the sketch normal (a point when perpendicular) |
| `{datum:"origin"}` | the projected world origin (point) |
| `{kind:"face", selector}` | every boundary edge of the face (unchanged) |
| `{kind:"edge", selector}` | the edge projected along the sketch normal; oblique straight edges stay one analytic line |
| `{kind:"edge", selector, mode:"pierce", near?:[u,v]}` | one point where the edge's 3D curve crosses the sketch plane (the isolated crossing nearest `near`, else nearest the edge midpoint); no crossing → `convert_failed`, reason `no plane crossing` |
| `{kind:"vertex", selector}` | the projected vertex (point) |
| `{kind:"sketch_entity", sketch, entity}` | another 2D sketch's solved entity, lifted to world and projected |

Every kind is associative: each mutating rebuild strictly re-resolves it (scored selector match
over all bodies, selectors.md thresholds) and marks it `stale` with last-good geometry when it no
longer resolves. `mode`/`near` persist in `source`.
