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

## Sheet Metal

The first sheet-metal feature (`sm_base_flange`) establishes the part as sheet metal: a
uniform-thickness sheet with a stored thickness + k-factor that later flanges inherit. Flanges
build TRUE solid geometry (a cylindrical bend region of the given inner radius + a flat tangent
wall, both at sheet thickness) and additionally record per-bend data for a future flat-pattern
unfold.

**Bend math** (canonical mm/deg). For inner radius `R`, thickness `T`, k-factor `K` (neutral fibre
at `K·T` into the thickness), and bend interior angle `θ`:
- neutral radius `Rn = R + K·T`
- **bend allowance** `BA = θ_rad · (R + K·T)` — the developed (flat) arc length of the neutral
  fibre through the bend; what the flat pattern consumes per bend.
- bend deduction `BD = 2·(R+T)·tan(θ/2) − BA` (the outside-setback convention; stored too).

### `sm_base_flange`
- params: `thickness: dim` (2) · `k_factor: dim` (0.45, 0..1) · `default_bend_radius: dim`
  (= thickness)
- refs: `profile: [sel(region)]` (≥1 closed sketch region)
- compiler: `features/sheetmetal.py::compile_sm_base_flange` → extrude the region(s) by
  `thickness`. Stores part-level `{thickness, k_factor, default_bend_radius}` on the body so edge
  flanges inherit them. `resolved`: `{thickness, k_factor, default_bend_radius, volume}`.
- errors: missing/open profile (no closed region); `thickness ≤ 0`; `k_factor` outside 0..1.

### `sm_edge_flange`
- params: `angle: dim` (90, in (0,180)) · `length: dim` (20, flat-wall length, EXCLUDING the bend
  region) · `radius: dim` (inner bend radius, default = sheet `default_bend_radius`) · `k_factor:
  dim` (inherited from the base flange) · `position: "material_inside"|"material_outside"|
  "bend_outside"` ("material_inside") · `flip: bool` (false)
- refs: `edge: sel(edge[line])` — a STRAIGHT edge on the sheet's thickness (side) face: the bend
  line.
- compiler: `features/sheetmetal.py::compile_sm_edge_flange` → builds a bend arc (inner radius `R`,
  sweep `angle`) tangent to the parent face + a flat wall of `length` tangent to the bend, at sheet
  thickness, as a cross-section prismed along the bend edge, then FUSED to the sheet body.
- `position` (mirrors SolidWorks "Flange Position", measured against the picked thickness-face
  plane): `material_inside` = the wall's OUTER surface is flush with that plane (bend eats inward,
  outside envelope unchanged); `material_outside` = the wall's INNER surface is flush (flange sits
  outside the original envelope); `bend_outside` = the bend's inner arc starts at the plane (bulges
  outward the most).
- `flip` reverses which side of the parent face the wall folds toward.
- Stored bend record (per flange, for the future flat pattern): `{feature, angle, radius,
  thickness, k_factor, neutral_radius, bend_allowance, bend_deduction, wall_length, position, flip,
  bend_line:[[x,y,z],[x,y,z]], e_dir, length_dir, fold_dir}`. `resolved`: `{angle, length, radius,
  k_factor, bend_allowance, bend_deduction, neutral_radius, volume}`.
- errors (all contained `FeatureError`): no base flange / non-sheet part; dangling or curved
  (non-straight) bend edge; edge not on a `~thickness`-wide face; `radius ≤ 0`; `length ≤ 0`;
  `angle ∉ (0,180)`; degenerate placement where the flange is not connected to the sheet (e.g.
  `bend_outside` folded away — the union stays disjoint).

### `sm_hem`
- params: `type: "closed"|"open"|"teardrop"` ("closed") · `length: dim` (hem return length,
  ~5·thickness; clamped to ≥1.5·thickness) · `radius: dim` (inner bend radius; default ≈
  thickness/2 for `closed` so the two skins sit a material-thickness apart, else = thickness) ·
  `k_factor: dim` (inherited from the base flange) · `flip: bool` (false; which side the hem folds
  toward).
- refs: `edge: sel(edge[line])` — a STRAIGHT thickness-face edge (the fold line), resolved exactly
  like the edge flange's bend edge.
- compiler: `features/sheetmetal.py::compile_sm_hem` → walks a CONSTANT-THICKNESS strip centerline
  (a lead-in run, one bend, a return wall) in the bend-edge frame and prisms it along the edge, then
  FUSES it to the sheet. Hem types differ only in the bend sweep + radius: `closed` = 180° fold at a
  tiny radius (≈ T/2) so the return wall folds flat back against the sheet (gap ≈ thickness);
  `open` = 180° bend at the requested `radius`, leaving a visible standoff (≈ 2·radius + T);
  `teardrop` = the bend curls PAST 180° (≈ 235°) at `radius` to nearly close a round teardrop.
- Stored bend record (one, same shape as the edge flange) with `angle` = the bend sweep
  (180° / 235°) and `BA = θ_rad·(R+K·T)`, so the flat pattern develops the hem. `resolved`:
  `{_type, angle, length, radius, k_factor, bend_allowance, neutral_radius, volume}`.
- errors (contained `FeatureError`): no base flange / non-sheet part; dangling or curved bend edge;
  edge not on a `~thickness`-wide face; `radius ≤ 0`; bad `type`; degenerate placement (the hem is
  not connected to / adds no material to the sheet).

### `sm_jog`
- params: `angle: dim` (90, the jog bend angle, in (0, 90]) · `offset: dim` (10, the Z-step the end
  flange is raised by) · `length: dim` (20, the flat end-flange length after the jog) · `radius:
  dim` (inner bend radius, default = sheet `default_bend_radius`) · `k_factor: dim` (inherited) ·
  `position: "material_inside"|"material_outside"|"bend_outside"` ("material_inside") · `flip: bool`
  (false).
- refs: `edge: sel(edge[line])` — the STRAIGHT thickness-face bend line (as the edge flange).
- compiler: `features/sheetmetal.py::compile_sm_jog` → TWO opposing bends: bend UP by `angle`, a
  short inclined connector that climbs the `offset`, then bend BACK by `angle` so the end flange is
  PARALLEL to the original face but offset from it by `offset`, then a flat run of `length`. Built as
  a constant-thickness strip centerline and fused to the sheet. The connector length is solved so
  the net perpendicular rise (the two bend arcs' rise + the inclined run) equals `offset`; if
  `offset` is smaller than the two bend radii already produce, the connector is clamped tiny and the
  achievable step is echoed in `resolved.offset`.
- Stores TWO bend records (the up bend + the back bend), each `BA = θ_rad·(R+K·T)`, so the flat
  pattern develops BOTH. `resolved`: `{angle, offset (achieved), length, radius, k_factor,
  bend_allowance, neutral_radius, volume}`.
- errors (contained `FeatureError`): no base flange / non-sheet part; dangling or curved bend edge;
  edge not on a `~thickness`-wide face; `radius ≤ 0`; `length ≤ 0`; `angle ∉ (0, 90]`; bad
  `position`; degenerate placement.

### `sm_flat_pattern`
- params: `{}` (none). refs: none. Operates on the RUNNING sheet-metal body plus the per-bend
  records the edge flanges stored (`ctx.datums["_sheet_metal_bends"]`).
- compiler: `features/sheetmetal_flat.py::compile_sm_flat_pattern` → UNFOLDS the folded part into
  its flat blank. The base flange is the fixed face; each flange is developed flat in the base
  plane. The bend records are SUFFICIENT — they carry `bend_allowance` (BA), the `bend_line`
  endpoints, and the `e_dir`/`length_dir`/`fold_dir` frame — so no re-derivation from the solid is
  needed. Method: rebuild the flat base block from the base broad face (the largest planar face ∥
  the base normal `e_dir × length_dir`; for `material_inside` it still spans the whole base), then
  for each bend lay a developed strip in that plane starting at the bend tangent line
  `tangent = bend_line[0] + length_dir·x0` and extending along `length_dir` by `BA + wall_length`
  (the bend region develops to BA; the wall keeps its length), at sheet thickness. `x0` is the
  flange-position setback mirroring the flange builder: `material_inside → −(R+T)`,
  `material_outside → −R`, `bend_outside → 0`. The result is a single planar sheet of `thickness`.
- **developed length** (the SolidWorks check): for one bend, the flat length along the flange
  direction = `base_length_to_bend + BA + wall_length`, where `base_length_to_bend` is the base
  extent minus the `|x0|` setback. The bend's projected/outer corner length is REPLACED by BA. For
  the canonical 60×40×2 + 90° flange (R=2, K=0.45, wall=20, `material_inside`): `x0 = −4`,
  base_to_bend = 36, `BA = (π/2)(2 + 0.45·2) = 4.5553`, `L_flat = 36 + 4.5553 + 20 = 60.5553 mm`.
- outputs: the FLAT solid (planar sheet) becomes the body. `resolved`:
  `{bend_count, thickness, flat_area, flat_volume, volume}` (`flat_area` = developed one-side broad
  area; `flat_volume` = `flat_area·thickness`). For the later flat-DXF / flat-view wave it stores
  `ctx.datums["_sheet_metal_flat"][fid] = {feature, thickness, bend_lines:[{feature, angle,
  bend_allowance, radius, line_start:[[x,y,z],[x,y,z]], line_end:[…]}], outline:[[x,y,z],…],
  flat_area, flat_volume, n_base, multibend}` — `bend_lines` are the per-bend flat lines (start =
  bend tangent, end = +BA), `outline` is the flat-blank boundary polyline.
- single vs multi-bend: single-level flanges (base + one or more edge flanges) unfold exactly. A
  flange off a flange (multi-level) is detected (its tangent line is off the base plane) and noted
  in `multibend`; it is developed at the correct size but its placement is best-effort.
- errors (contained `FeatureError`): no body / non-sheet-metal part; no bends (a flat sheet is
  already its own flat pattern — nothing to unfold); empty developed body.

## Weldments

A `weld_member` sweeps a structural PROFILE from the small profile library along the segments of a
sketch, producing weldment members (one solid per segment) with mitre corner trim and a stored
per-member length for the future cut list. Gusset / end-cap / weld-bead and the cut-list TABLE are
later waves. Canonical units mm/deg.

### Profile library (`engine/doc/weld_profiles.py`)
Each profile is a 2-D cross-section (outer boundary minus any inner void — hollow tubes are
genuinely hollow), drawn centred on its local origin and swept along +Z by the member compiler.
Dimension keys (mm) with their defaults:
- `square_tube`: `size` (20) · `thickness` (2) — hollow square. area = `size² − (size−2t)²`.
- `rect_tube`: `w` (40) · `h` (20) · `thickness` (2) — hollow rectangle. area = `w·h − (w−2t)(h−2t)`.
- `round_tube`: `od` (20) · `thickness` (2) — hollow round. area = `π((od/2)² − (od/2−t)²)`.
- `angle_L`: `leg` (30) · `thickness` (3) — equal-leg L (true L, not a filled square).
  area = `2·leg·t − t²`.
- `flat_bar`: `w` (40) · `thickness` (6) — solid bar. area = `w·t`.
- `c_channel`: `h` (50) · `w` (25) · `thickness` (3) — open C-channel (opening +X).
  area = `h·w − (h−2t)(w−t)`.

The analytic area of every profile matches its built-face area exactly (the cut-list mass roll-up
depends on this).

### `weld_member`
- params: `profile: str` (a library name; required) · per-profile dimension keys (mm; missing keys
  fall back to the library default) · `angle: dim` (0; profile rotation about the path) ·
  `justify: "center"|"corner"` ("center"; SolidWorks "Locate Profile" — `center` centres the
  section on the segment, `corner` hangs its lower-left bbox corner on the segment) ·
  `trim_corners: bool` (true).
- refs: `sketch: feature-id` (the source sketch; its raw OPEN line/arc segments become the member
  paths — closed loops like circles are skipped) · `segments: [entity-id]?` (default ALL segments).
- compiler: `features/weldment.py::compile_weld_member`. Per segment: build the profile face,
  orient it perpendicular to the segment at its start (profile plane normal = path tangent), sweep
  along that one segment via OCCT `BRepOffsetAPI_MakePipe` → one member solid. Members are kept as
  SEPARATE bodies (an assembly of members), each appended to the rolling body list so it is
  individually addressable for the cut list.
- corner trim (joint type = **mutual mitre**): where two STRAIGHT members share a sketch vertex,
  both are cut by the mitre plane through that vertex (the plane bisecting the two outgoing
  tangents); each member is subtracted by the half-space on the OTHER member's side, so neither
  crosses the plane — a clean mitre with no interpenetration (pairwise intersection volume ≈ 0).
  Best-effort: a kernel hiccup leaves the member untrimmed rather than failing the feature.
  Limitations: straight (line) segments only (arcs sweep but butt at their ends); a 3+-member
  vertex trims each adjacent pair independently (no single fitted gusset); near-collinear members
  (≈180°) are left untrimmed.
- cut-list data: per-member records `{segment_id, profile, length, start, end, area, volume}` are
  stashed in `ctx.datums["_weld_members"][feature_id]` (`{profile, dims, members:[…]}`); the
  summary `{_member_count, _total_length, _total_volume, _section_area}` is echoed in `resolved`.
- errors (all contained `FeatureError`): unknown profile name; sketch missing / not compiled;
  sketch has no usable (open) segments; degenerate (zero-length) segment; degenerate profile
  dimensions (e.g. wall ≥ half the tube).

### `weld_gusset` (dress)
A triangular/polygonal reinforcing plate welded into an L-corner between two members. It is a
SEPARATE weldment body (welded in, not fused into a member).
- params: `thickness: dim` (5) · `d1: dim` (30; leg along member 1) · `d2: dim` (30; leg along
  member 2) · `type: "triangular"|"polygonal"` ("triangular"; polygonal clips the outer hypotenuse
  tip to a cut-corner pentagon) · `location: "centered"|"inner"|"outer"` ("centered"; shifts the
  plate ±½·thickness off the shared plane) · `offset: dim` (0; extra shift along the plane normal).
- refs: `faces: [sel(face)]` — **exactly two** planar face selectors, one on each member at the
  corner (the two coplanar faces facing into the corner, e.g. the members' top faces). Faces resolve
  against ALL current member bodies (members are separate bodies).
- compiler: `features/weldment.py::compile_weld_gusset`. Recovers each member's axis as the picked
  face's longest in-plane edge; the corner apex = the near-intersection of the two axis lines; each
  leg points from the apex toward its face centre; the plate (right triangle `apex, apex+d1·leg1,
  apex+d2·leg2`, or the cut-corner pentagon for `polygonal`) is extruded by `thickness` along the
  shared-plane normal and appended as a new body. `resolved`: `{thickness, d1, d2, _plate_area,
  _triangle_area, volume}`.
- supported: the common L-corner of two non-parallel members (any orientation, recovered from the
  picks). Limitation: a member-to-face or 3-member junction is not specially fitted — pick the two
  faces whose shared plane you want the gusset to lie in.
- errors (contained): not exactly two faces; a non-planar pick; faces whose member axes are
  parallel/collinear (not a corner); a face selector that resolves on no body (dangling);
  thickness/leg ≤ 0; degenerate plate.

### `weld_end_cap` (dress)
A plate closing the open END of a hollow member (a tube). SEPARATE weldment body.
- params: `thickness: dim` (5) · `type: "outer"|"inner"` ("outer" mounts a plate covering the whole
  tube end ON the end face and extrudes outward; "inner" insets a plate matching the bore INTO the
  tube) · `offset: dim` (0; shift along the member axis) · `overhang: dim` (0; outer only — grows the
  plate past the profile on every side).
- refs: `face: sel(face)` — one planar member END face (its outline is the member's cross-section).
  Resolves against all member bodies.
- compiler: `features/weldment.py::compile_weld_end_cap`. Outer: a plate from the end face's OUTER
  boundary (covers the whole end incl. the bore), optionally grown by `overhang`, extruded outward by
  `thickness`. Inner: a plate from the end face's INNER boundary (the bore), inset into the tube by
  `thickness`. `resolved`: `{thickness, overhang, _cap_area, volume}`.
- supported: square/rect/round hollow tubes (outer + inner) and solid sections (outer only).
- errors (contained): missing/dangling face; non-planar face; `inner` on a solid section (no bore);
  thickness ≤ 0; negative overhang; bad `type`.

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
