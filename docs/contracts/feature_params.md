# Contract: Feature Parameters — v1

> The single source of truth for every feature's `params`/`refs`, consumed by THREE parties:
> Python compilers (`engine/doc/features/*`), C++ PropertyManager forms, and the M9 AI modeler's
> few-shot examples. Field order here IS the PM form order.
>
> Notation: `dim` = dimension value object `{value, expr}` (doc_schema §0). `sel(kinds…)` =
> selector restricted to those kinds. `[x]` = array. Defaults in parentheses.

## Datums

### `plane`
- params: `method: "offset"|"angle"|"three_point"|"midplane"` ("offset") · `offset: dim` (10) ·
  `angle: dim` (45, method=angle) · `flip: bool` (false)
- refs: `base: sel(face[plane], datum)` (offset/angle/midplane #1) · `second: sel(face, datum,
  edge)` (angle hinge edge; midplane #2) · `points: [sel(vertex)]×3` (three_point)
- compiler: `features/datum.py::compile_plane` → build123d `Plane`
- errors: non-planar base; colinear points.

### `axis`
- params: `method: "cylinder"|"two_points"|"edge"|"plane_intersect"` ("cylinder")
- refs: `a: sel(face[cylinder,cone], vertex, edge, datum)` · `b: sel(vertex, datum)?`
- compiler: `compile_axis` → `Axis`

### `point`
- params: `method: "vertex"|"center"|"on_face"` · `uv: [f,f]?` (on_face)
- refs: `target: sel(vertex, edge[circle], face)`

### `csys`
- params: — · refs: `origin: sel(vertex, datum)` · `x_dir: sel(edge, datum)?` · `y_dir: …?`

## Sketch

### `sketch`
- params: `entities: [Entity]` · `constraints: [Constraint]` (doc_schema §3)
- refs: `plane: sel(face[plane], datum)`
- compiler: `features/sketch.py` — solve → wire graph → closed-loop region detection →
  pickable region faces (AICM pseudo-body `sketch:<id>`).

## Primitives (AI/quick-start path; SolidWorks purists use sketch+extrude)

### `box`
- params: `length: dim` (20) · `width: dim` (20) · `height: dim` (20) · `centered: bool` (true)
- refs: `at: sel(datum, vertex)?` (default origin)
### `cylinder` — `radius: dim` (10) · `height: dim` (20) · `centered: bool` (true); refs `at?`
### `sphere` — `radius: dim` (10); refs `at?`
### `cone` — `r1: dim` (10) · `r2: dim` (0) · `height: dim` (20); refs `at?`
### `torus` — `major_r: dim` (15) · `minor_r: dim` (3); refs `at?`
- compiler: `features/primitives.py`; first solid-producing feature creates Body1, later ones
  default `merge:true` (fuse) — params include `merge: bool` (true) · `mode: "boss"|"cut"` ("boss").

## Sketched features

### `extrude`
- params: `mode: "boss"|"cut"` ("boss") · `end: "blind"|"through_all"|"mid_plane"` ("blind") ·
  `depth: dim` (10, blind) · `flip: bool` (false) · `draft: dim` (0) · `draft_outward: bool`
  (false) · `merge: bool` (true)
- refs: `profile: [sel(region, face[plane])]` (≥1)
- compiler: `features/extrude.py` → `extrude(...)`; draft via `BRepOffsetAPI_DraftAngle` post-op;
  cut = boolean subtract; through_all = 1.05× bbox extent both ways as needed.
- errors: open profile (region detection failed); cut removes nothing (warn).

#### `extrude` — C26-B additions (implemented; supersede where noted)
- `end` extended: now `"blind"|"through_all"|"mid_plane"|"up_to_next"`. `up_to_next` extrudes
  until the first surface of the existing body along the direction (requires a body — clean
  error `"extrude: up_to_next requires an existing body"` otherwise).
- Second direction (opposite of direction 1, which `flip` controls):
  `end2: "none"|"blind"|"through_all"|"up_to_next"` ("none") · `depth2: dim` (10, used when
  `end2 == "blind"`). When `end2 != "none"` a second tool is built from the same profiles along
  the negated direction and applied together (boss: union both; cut: subtract both).
- `mid_plane` semantics confirmed/normative: `depth` is the TOTAL height (±depth/2 each side of
  the sketch plane). (Implementation already did this; verified, kept.)
- Draft is now IMPLEMENTED (no longer deferred) via build123d `extrude(..., taper=°)`
  (`Solid.extrude_taper`). Sign convention: default inward draft (`draft_outward=false`) →
  POSITIVE taper (profile narrows toward the extrude direction, smaller top, matching SolidWorks
  default); `draft_outward=true` → NEGATIVE taper (profile grows). Draft applies to `blind` and
  `mid_plane` only; for `through_all`/`up_to_next` it is IGNORED with a resolved note
  `resolved._draft_note` (SolidWorks greys it out there too).
- `resolved` (echoed for the PM to display) is extended: `{"depth": effective_amount,
  "taper": <signed °>, "depth2": <amount2 when end2 != none>, "_draft_note": <when ignored>}`.
- up_to_next realization: boss uses the library path `extrude(face, until=Until.NEXT,
  target=body, dir=…)` (void-fill to next face); cut uses a manual nearest-solid-slab subtract
  (a full-length linear column ∩ body, nearest slab along the cut direction) because the library
  `Until.NEXT` fills the next void and isn't directly subtractable.

### `revolve`
- params: `mode: "boss"|"cut"` · `angle: dim` (360) · `flip: bool` (false) · `merge: bool` (true)
- refs: `profile: [sel(region)]` · `axis: sel(edge[line], datum, region-line)` — axis must not
  cross profile interior (error).
- compiler: `features/revolve.py` → `revolve(...)`

### `sweep`
- params: `mode: "boss"|"cut"` · `orientation: "follow_path"` (frozen v1) · `merge: bool`
- refs: `profile: [sel(region)]` · `path: sel(edge, region-chain)` (sketch wire or model edge chain)
- compiler: `features/sweep.py` → `sweep()` / `BRepOffsetAPI_MakePipeShell`
- errors: self-intersecting sweep (OCCT failure surfaced with hint).

### `loft`
- params: `mode: "boss"|"cut"` · `ruled: bool` (false) · `merge: bool`
- refs: `sections: [sel(region, face)]` (≥2, ordered; PM well preserves pick order)
- compiler: `features/loft.py` → `loft()` / `ThruSections`

### `helix` (curve feature; consumed by sweep)
- params: `pitch: dim` (5) · `height: dim` (20) · `turns: dim?` (null → height/pitch) ·
  `taper: dim` (0) · `start_angle: dim` (0) · `ccw: bool` (true)
- refs: `base: sel(edge[circle], face[cylinder])`
- compiler: `features/curves.py` → `Helix(...)`; produces an edge body (pickable as sweep path).

## Dress-up

### `fillet`
- params: `radius: dim` (2)
- refs: `edges: [sel(edge)]` (≥1)
- compiler: `features/dress.py::compile_fillet`; **auto-clamp retry** (halve ×3, floor 0.2 mm,
  warning surfaced) — generalizes the proven `kernel._wrap_edge_op` behavior.

#### `fillet` — C30-A additions (implemented; variable size)
- Optional `radius2: dim` (absent/0/equal-to-radius → constant path, untouched). When present and
  `!= radius`, the fillet becomes **variable size**: `radius` applies at each edge's START vertex,
  `radius2` at its END vertex, with a linear radius law between them. build123d has no
  variable-radius wrapper, so this path drops to raw OCP `BRepFilletAPI_MakeFillet.Add(r1, r2,
  edge)` and re-wraps the result as a build123d `Solid`.
- Edge START/END come from OCCT topology direction and may feel arbitrary (SolidWorks shows a
  draggable handle per vertex; v1 exposes the two numbers and documents this honestly — to flip
  which end gets which radius, swap the two values).
- Auto-clamp retry: on OCCT failure, BOTH radii halve in lockstep (×3, floor 0.2 mm on either).
- `resolved` gains `radius2` (effective end radius after any clamping) on the variable path.

### `chamfer`
- params: `length: dim` (1) · `kind: "equal"` (frozen v1; `two_dist`/`dist_angle` reserved)
- refs: `edges: [sel(edge)]`

### `shell`
- params: `thickness: dim` (2) · `outward: bool` (false)
- refs: `remove: [sel(face)]` (may be empty = hollow closed shell)
- compiler: `offset(solid, -t, openings=faces)`; OCCT failures get the guidance string
  ("shell before fillet often succeeds where after fails").

### `draft`
- params: `angle: dim` (2)
- refs: `neutral: sel(face[plane], datum)` · `pull: sel(face[plane], datum, edge)` (direction
  source; its normal/dir) · `faces: [sel(face)]`
- compiler: `BRepOffsetAPI_DraftAngle` (NOT build123d `Draft` — that's a 2D helper).

### `hole`
- params: `style: "simple"|"cbore"|"csink"|"tap"` ("simple") · `standard: "M2"…"M12"|"custom"`
  ("M3"; table sets diameter, cbore/csink dims per ISO) · `diameter: dim` (3.4, custom) ·
  `end: "through_all"|"blind"` · `depth: dim` (10, blind) · `cbore_d: dim?` · `cbore_depth: dim?`
  · `csink_d: dim?` · `csink_angle: dim` (90) · `thread_callout: str?` (cosmetic, BOM/callout only)
- refs: `face: sel(face[plane])` · `positions: sel(region) | [sel(vertex)]` — a sketch of points
  on that face (preferred: `positions_sketch: feature-id`)
- compiler: `features/hole.py` → `Hole/CounterBoreHole/CounterSinkHole` at each point.

## Patterns & mirror — tool-body semantics (normative)

The compiler caches each feature's **tool body** (the solid it fused or subtracted). Patterns
transform the SEED FEATURES' tool bodies and re-apply the same boolean per instance — this is
"geometry pattern" semantics: fast, robust, no per-instance re-solve.

### `linear_pattern`
- params: `count1: int` (2, ≥1) · `spacing1: dim` (20) · `count2: int` (1) · `spacing2: dim` (20)
  · `reverse1: bool` · `reverse2: bool`
- refs: `dir1: sel(edge[line], datum-axis)` · `dir2: …?` · `features: [feature-id]` (≥1, must
  precede this feature)

### `circular_pattern`
- params: `count: int` (4) · `angle: dim` (360, total) · `equal_spacing: bool` (true) ·
  `reverse: bool`
- refs: `axis: sel(edge, face[cylinder], datum-axis)` · `features: [feature-id]`

### `mirror`
- params: `merge: bool` (true)
- refs: `plane: sel(face[plane], datum)` · `features: [feature-id]` OR `bodies: [sel(body)]`
  (exactly one of the two)

### Assembly patterns: `asm_linear_pattern` / `asm_circular_pattern`
- Same params; refs `components: [component-id]` instead of features. Live in
  `AssemblyDocument.features`.

## Multi-body

### `boolean`
- params: `op: "combine"|"subtract"|"intersect"` · `keep_tools: bool` (false)
- refs: `targets: [sel(body)]` (≥1) · `tools: [sel(body)]` (≥1, subtract/intersect)

## Escape hatches

### `script`
- params: `code: str` (build123d; executes in the existing forgiving kernel namespace; its
  `result`/`parts` become this feature's bodies — the legacy agent path lands here unchanged)
- refs: —

### `import`
- params: `source_path: str` · `format: "step"|"iges"|"brep"|"stl"` · `original_name: str?`
- refs: — · bodies are frozen (no upstream params).

## Stretch (schema-reserved): `rib`, `split`

`rib {params: thickness: dim, flip: bool; refs: profile: sel(region-open), …}` ·
`split {refs: tool: sel(face, datum); params: keep: "both"|"above"|"below"}`

## PM form conventions (all features)

Title = feature display name; header buttons OK (✓ accent) / Cancel (✗). Selector wells show
pick count + kind chips, active well highlights, right-click → clear. `dim` fields accept unit
suffixes (`25`, `1.5in`, `3/8in`) and `=expr` to bind an expression. Tab order = field order
above. Live preview: PM edits issue transient rebuilds (same path as `edit_feature`, not
committed until OK; Cancel restores via worker undo of the preview op).
