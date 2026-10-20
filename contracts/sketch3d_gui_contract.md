# Contract: 3D Sketch (`sketch3d`) — GUI / RPC surface — v1

> The GUI-environment agent builds the 3D sketcher from THIS FILE ALONE.
> Engine side: `core/sketch/sketch3d.{h,cpp}` (solver), `core/geom/profile3d.{h,cpp}` (wire
> chaining), `core/doc/document.cpp` (`sketch3d` compile + sweep spine/guide consumption),
> `core/doc/session.cpp` + `core/api/libchudware.cpp` (RPCs). Schema is normative in
> `doc_schema.md §3B` and `feature_params.md §sketch3d`; this file restates it with exact
> request/response JSON. Tests proving every flow below: `core/tests/test_sketch3d.cpp`
> (solver) and `core/tests/test_sketch3d_doc.cpp` (doc compile, sweep, RPC round-trip).

## 0. Model

- A `sketch3d` is an ordinary FEATURE (`type:"sketch3d"`) in the part's `features` history.
  It has **no plane and no refs** — all coordinates are **world millimeters**.
- It never produces a body. It compiles to **3D wire chains** that downstream features
  reference (v1 consumer: `sweep` path + guide; loft guides reserved).
- It participates in `doc_apply_ops` exactly like any feature: `add_feature`,
  `edit_feature`, `delete_feature`, undo/redo, suppression, rollback bar. There are **no
  sketch3d-specific ops** — entity/constraint edits are `edit_feature` params patches.
- Two dedicated read-only RPCs exist: `sketch3d_health` and `sketch3d_drag` (both
  STATELESS — no enter/exit session mode; they solve from the persisted feature params on
  every call).

## 1. Entity JSON (params.entities — array)

All entities: `id` (`"e<N>"`, unique in the sketch), `type`, `construction:bool`.

```json
{"id":"e1","type":"point","construction":false,"p":[10,0,5]}
{"id":"e2","type":"line","construction":false,"p1":[0,0,0],"p2":[0,0,80]}
{"id":"e3","type":"arc","construction":false,"p1":[0,0,80],"pm":[2.93,0,87.07],"p2":[10,0,90]}
{"id":"e4","type":"spline","construction":false,"points":[[10,0,90],[15,2,91],[20,0,92]]}
```

- **arc is THREE-POINT**: `p1` start, `pm` interior "through" point, `p2` end. The circle is
  the circumcircle of the three points. Collinear points = degenerate (the feature compiles,
  that edge is skipped; solver `point_on(arc)` goes inert). There is NO center/normal/radius
  storage — the GUI derives center/radius for display from the three points.
- **spline** interpolates THROUGH every point (cubic), ≥ 2 points. No `degree` field in v1.
- `point` entities never form edges/wires (annotation/skeleton anchors, constraint targets).
- `construction:true` entities never join wire chains.

### 1a. `eq_curve3d` (T-3DEQ, equation-driven curve)

```json
{"id":"e5","type":"eq_curve3d","construction":false,
 "xt":"R*cos(t)","yt":"R*sin(t)","zt":"P*t/360","t1":0,"t2":720}
```

- `xt`/`yt`/`zt`: expression strings over `t`, evaluated per-sample against the document
  equation grammar (doc_schema §4 — degrees trig, `env` globals; `t` itself is NOT a global,
  it's bound per-sample). `t1`/`t2`: the parameter range (numbers or `env`-resolvable
  expressions via the normal dimension-value path).
- **0 DOF — equation-fixed**, exactly like the 2D `eq_curve`: the endpoints (`e5.p1` at `t1`,
  `e5.p2` at `t2`) are pinned by an implicit fix residual the instant the entity is allocated,
  so no other constraint can move them; they ARE valid `coincident`/`distance`/`point_on`
  targets for chaining onto adjacent geometry. The GUI should render eq_curve3d exactly like a
  `spline` (it compiles the same way — sampled + BSpline-interpolated) but should NOT offer
  point-drag handles on it beyond its two endpoints (no interior control points exist).
- Display: use the compiled wire's tessellation for the curve shape; do not attempt to derive
  a closed form from `xt/yt/zt` in the GUI.

## 1b. External model references (T-3DXREF — ground a constraint to a model vertex)

```json
"external_refs": [
  {"id":"x1", "source":{"kind":"vertex","selector":{...}}, "stale": false,
   "last_point":[12.5,40.0,0.0]}
]
```

- Lives on the feature at `params.external_refs` (same array-of-records shape as `entities`/
  `constraints`; full field semantics: doc_schema §3B.1b). `x1` is a **point handle** — pass it
  directly in any constraint's `refs`, e.g. `{"type":"coincident","refs":["e3.p2","x1"]}`.
- **v1 supports `source.kind:"vertex"` only.** `"edge"`/`"face"` are accepted by the schema for
  forward-compat but are NEVER resolved in v1 (always `stale:true`) — don't let the GUI offer
  edge/face picks as an external-ref source yet; vertex-only.
- **Creating one (GUI flow):** the host already has, from its id-pass, the picked vertex's
  `body_id` + `topo_id` (kind `"vertex"`) — the exact pair `doc_resolve_pick` takes. Either (a)
  call `doc_resolve_pick(doc_id, body_id, "vertex", topo_id)` to get the `selector`, then
  `doc_apply_ops` `edit_feature` on the sketch3d with `params.external_refs` appended (mint the
  next unused `"x<N>"` id, `stale:true` initially — it resolves on the next rebuild), or (b) go
  through the MCP tool `sketch3d_ref_vertex(sketch3d_id, body_id, topo_id)` which does both
  steps and force-rebuilds so `last_point` comes back populated immediately.
- **Re-resolution:** happens automatically on every mutating `doc_apply_ops` batch and on
  `doc_rebuild` (`refresh_sketch3d_external_refs`, before compile) — the GUI does not need to
  do anything special after the initial creation; a normal edit/rebuild refreshes `last_point`/
  `stale` on every affected sketch3d feature.
- **Stale display:** `sketch3d_health` surfaces a stale ref as a feature-status `warning`
  string (never a hard failure) — show it the same way the 2D external-ref stale banner is
  shown; the ground point stays at its last-good position so the constrained geometry doesn't
  jump/disappear.

## 2. Handles (constraint refs + drag targets)

| handle | meaning |
|---|---|
| `"e3"` | whole entity (valid for `fix`, and for bare-entity refs in `distance`/`length`/`parallel`/…) |
| `"e3.p"` | point entity's point |
| `"e3.p1"` / `"e3.p2"` | line or arc endpoints (aliases `"e3.start"` / `"e3.end"` accepted) |
| `"e3.pm"` | arc through-point (alias `"e3.mid"`) |
| `"e3.p0"` … `"e3.pN"` | spline interpolation points |

Grammar: `^e\d+(\.(p|p1|p2|pm|start|end|mid|p\d+))?$`. Every point handle is 3 solver
variables (x, y, z) and a valid drag target.

## 3. Constraint JSON (params.constraints — array)

All constraints: `id` (`"c<N>"`, unique), `type`, `refs:[handle,…]`. Dimensional constraints
carry `value: {"value": <float>, "expr": <string|null>}` (expr evaluated against document
equations; canonical dim object, doc_schema §0) and accept `driven:bool` (true = reference
dimension, measured not enforced).

| type | refs | value | notes |
|---|---|---|---|
| `coincident` | `[pointH, pointH]` | — | pulls the two points together (3 residuals) |
| `fix` | `[handle or entity]` | — | pins at the AUTHORED coords |
| `distance` | `[pointH, pointH]` or `[pointH, "e<N>"(line)]` | mm | point-line = perpendicular gap to the **infinite** line |
| `length` | `["e<N>"(line)]` | mm | endpoint-to-endpoint |
| `parallel` | `[lineE, lineE]` | — | removes 2 DOF |
| `perpendicular` | `[lineE, lineE]` | — | removes 1 DOF |
| `angle` | `[lineE, lineE]` | deg `[0,180]` | direction-sensitive dot (as authored) |
| `along_axis` | `[lineE]` | — | extra record field: `"axis":"x"|"y"|"z"` **or** `"dir":[x,y,z]` |
| `point_on` | `[pointH, "e<N>"(line or arc)]` | — | line = infinite line; arc = full circumcircle (range-free) |

```json
{"id":"c1","type":"fix","refs":["e2.p1"]}
{"id":"c2","type":"along_axis","refs":["e2"],"axis":"z"}
{"id":"c3","type":"length","refs":["e2"],"value":{"value":80.0,"expr":"H"},"driven":false}
{"id":"c4","type":"coincident","refs":["e2.p2","e3.p1"]}
```

**Deferred to v2** (validator rejects / solver skips): `point_on` a spline, `tangent`,
`symmetric`, `equal`, `midpoint`. Unknown constraint types are skipped forward-tolerantly by
the solver but REJECTED by `doc_apply_ops` validation — the GUI must only emit the table
above.

## 4. RPCs

Worker transport: same as every doc RPC (method + params JSON through the C ABI /
worker_rpc). The sketch3d feature id travels in the `"sketch"` param (mirrors `sketch_*`).

### 4.1 add / edit / delete (generic doc ops — no special ops)

```json
{"doc_id":"…","base_revision":-1,"ops":[
  {"op":"add_feature","feature":{"type":"sketch3d","name":"3DSketch1","params":{
     "entities":[{"id":"e1","type":"line","construction":false,"p1":[0,0,0],"p2":[0,0,80]}],
     "constraints":[{"id":"c1","type":"fix","refs":["e1.p1"]}]}}}
]}
```
Response: standard `doc_apply_ops` envelope (revision, id_map `$0 -> "f7"`, tree,
rebuild.feature_status). Validation errors (`invalid_op`) name the bad field, e.g.
`features[0](f7).params.constraints[0].value: required for distance`.

Edit = params patch (patch semantics: keys REPLACE; send whole arrays):
```json
{"op":"edit_feature","id":"f7","params":{"entities":[ …full array… ]}}
```
Delete: `{"op":"delete_feature","id":"f7"}`.

### 4.2 `sketch3d_health` — solve + status snapshot (read-only)

Request: `{"method":"sketch3d_health","params":{"doc_id":"…","sketch":"f7"}}`

```json
{
  "ok": true,
  "status": "under",              // "under" | "full" | "over" (over = conflict/non-convergent)
  "dof": 3,
  "converged": true,
  "statuses": {"e1": "full", "e2": "under"},
  "redundant": ["c9"],            // constraint ids
  "conflicting": [],              // constraint ids
  "free": [{"entity":"e2","param":"p2 x","var":9},
           {"entity":"e2","param":"p2 y","var":10},
           {"entity":"e2","param":"p2 z","var":11}],
  "entities": [ {"id":"e1","type":"line","construction":false,
                 "p1":[0,0,0],"p2":[0,0,80]}, … ],   // SOLVED coords, array form
  "wires": [ {"closed": false, "n_edges": 3} ]        // chain report, compiler order
}
```

- `free[].param` names the loose coordinate in plain words: `"p x|y|z"` (point),
  `"p1|p2 x|y|z"` (line), `"p1|pm|p2 x|y|z"` (arc), `"p<K> x|y|z"` (spline).
- `wires` order == the order the sweep compiler sees (`wire 0` = chain containing the
  earliest-authored entity), so the GUI's wire picker index maps 1:1 to the `{"wire":k}` ref.
- Errors (unknown fid / wrong type) throw the standard OpError envelope:
  `{"error":{"code":"invalid_op","message":"no sketch3d feature 'f9'", …}}`.

### 4.3 `sketch3d_drag` — constrained drag (read-only / TRANSIENT)

Request (single handle):
```json
{"method":"sketch3d_drag","params":{
  "doc_id":"…","sketch":"f7","handle":"e2.p2","xyz":[20.0,5.0,5.0]}}
```
Multi-target (wins over handle/xyz):
```json
{"method":"sketch3d_drag","params":{
  "doc_id":"…","sketch":"f7",
  "targets":[{"handle":"e2.p2","xyz":[20,5,5]},{"handle":"e3.pm","xyz":[4,4,9]}]}}
```

Response (success):
```json
{
  "ok": true,
  "entities": [ …array form, solved coords after the pull… ],
  "statuses": {"e1":"full","e2":"under"},
  "rejected_handles": [],
  "converged": true,
  "budget_exceeded": false        // 10 ms solver budget hit (still best-so-far coords)
}
```
Response (all targets unresolvable):
```json
{"ok": false, "error": "no drag targets resolved",
 "rejected_handles": ["e9.p1"],
 "entities": [ …FROZEN last coords — never wipe the overlay… ],
 "statuses": {}}
```

Semantics (mirrors 2D `sketch_drag`):
- Soft-target pull at weight 0.01 — every real constraint dominates. Fixed/constrained
  points stay put; the drag distributes through the constraint system.
- STATELESS + TRANSIENT: nothing is persisted. Call per mouse-move.
- **Commit on mouse-up** = plain `edit_feature` with the last drag response's entities:
  `{"op":"edit_feature","id":"f7","params":{"entities":[ …response.entities… ]}}`
  (strip nothing; `id`/`type`/`construction` round-trip as returned). There is no
  `sketch3d_drag_commit` — the generic op IS the commit, keeping it one undo step.

## 5. Referencing sketch3d wires (sweep / downstream)

- Sweep spine: `{"op":"add_feature","feature":{"type":"sweep","params":{},"refs":{
    "profile":[{"kind":"region","sketch":"<2D-profile-fid>"}],
    "path":"<sketch3d-fid>"}}}` — bare fid works when the sketch3d has exactly ONE chain.
- Multi-chain sketch3d: `"path": {"feature":"<fid>","wire":1}` (index from
  `sketch3d_health.wires`). Bare-fid on a multi-chain sketch fails loudly:
  `sweep: path sketch3d has 2 wire chains — pick one with {"feature":"<fid>","wire":<k>}`.
- Guide curve: `"guides": [{"feature":"<fid>","wire":0}]` (0 or 1 guide; must differ from
  the path; the guide drives profile orientation + scaling — SolidWorks Guide Curves).
- The profile must sit at/near the spine start, roughly perpendicular to its tangent
  (SolidWorks pierce rule); a zero-volume sweep error names the actual gap distance.
- Loft `guides` do NOT consume sketch3d yet (loft compiler limitation, reserved).

## 6. Rendering / display notes

- The GUI draws entities from `sketch3d_health.entities` (or the last drag response) — the
  document stores last-committed coords, so a fresh doc opens displayable without solving.
- Arc display: derive center/radius from the circumcircle of `p1/pm/p2`.
- `statuses` colors: `under` (default), `full` (fully-constrained color), `over`
  (conflict red) — same palette as the 2D sketcher.
- `wires[].closed` tells the GUI whether a chain loops (closed 3D wires are valid; they are
  still only path/guide material in v1 — no face/region from a sketch3d).

## 7. Known limitations (v1, honest list)

1. Arc solving with `point_on(arc)` uses a numeric Jacobian (central difference) — fine at
   interactive sizes; everything else is analytic.
2. `point_on` spline, `tangent`, `symmetric`, `equal`, `midpoint` deferred (v2).
3. Sweep accepts ONE guide (MakePipeShell drives one auxiliary spine); loft guides reserved.
4. No region/face detection from closed 3D chains (no "3D profile" extrude).
5. `sketch3d_drag` re-solves cold per call (no warm session state). At v1 sketch sizes
   (≤ ~200 vars) this is well under the 10 ms budget; a session-warm path is a later
   optimization, API-compatible.
6. MCP `sketch3d_*` tools (services/mcp/sketch3d_tools.py) still speak the SESSION-scoped
   Python store, not this feature — re-pointing map in that file's header TODO.
7. External refs (T-3DXREF, §1b): only `source.kind:"vertex"` resolves in v1; `"edge"`/`"face"`
   sources are schema-legal but always report `stale:true` — a documented v1 gate, not a bug.
8. External-ref resolution targets the document's FINAL compiled body/bodies (not strictly
   "the body as of this sketch3d's position in the feature tree") — see doc_schema §3B.1b for
   the exact wording; in practice this is usually the more useful behavior.

## 8. GUI v1 — working-plane scheme (host/src/ui/sketch3d_mode.{h,cpp})

How the desktop 3D sketcher places free-space points (documented here per the GUI work order):

- Points are placed by ray-casting the cursor onto a **movable working plane**. Presets, cycled
  with **Tab** or the overlay-toolbar buttons: `XY` (default), `XZ`, `YZ`, `View` (view-aligned,
  re-derived from the live camera every frame).
- The plane is **anchored at the last placed point** of the in-progress tool (the origin when
  idle), so a polyline chain can turn in space by switching presets mid-chain.
- **Snapping** (beats the plane): existing sketch3d point handles + the world origin within
  10 px — the click lands on the exact 3D point and a `coincident` constraint is emitted
  (origin snap is positional only). **Axis inference**: with a previous tool point, the
  candidate is pulled onto the X/Y/Z line through it when within 6 px on screen; an
  axis-snapped Line segment gets a real `along_axis` constraint.
- **Camera**: NOT plane-locked (free-space). MMB orbit / Ctrl+MMB pan / Shift+MMB+wheel zoom
  stay live — but ALL camera input freezes while an LMB entity drag is armed/live (dead-zone
  rule; DPI-scaled 5 px arm threshold, quadratic edge auto-pan is the only view motion).
- **Drag**: handle drags move on the screen-parallel plane through the handle's press position;
  transient `sketch3d_drag` once per frame, committed on release as ONE `edit_feature` (§4.3).
- Gated in GUI v1 (honest list): repositioning the working plane onto a picked model FACE,
  snapping to model (B-rep) vertices, in-viewport wire picking for sweep wells (sketch3d wires
  are picked from the FEATURE TREE into path/guide wells; multi-chain `{feature,wire:k}` refs
  need the tree->health wire index flow, not yet surfaced).

## 9. Amendment 2026-10-16 — v2 relation set, skipped report, GUI vertex references

Supersedes the "Deferred to v2" paragraph of §3, items 2 and 7 (vertex part) of §7 and the
"snapping to model (B-rep) vertices" gate of §8.

**New constraint types** (validator `validate_sketch3d_params` and solver `sketch3d.cpp`):

| type | refs | extra field | meaning |
|---|---|---|---|
| `tangent` | `[lineE|arcE|splineEnd, arcE|splineEnd]` | — | tangent directions parallel at the shared (coincident) end; line↔spline uses the spline end |
| `equal` | `[lineE, lineE]` or `[arcE, arcE]` | — | equal lengths / equal radii |
| `midpoint` | `[pointH, lineE|arcE]` | — | point at the line middle / arc mid-sweep |
| `collinear` | `[lineE, lineE]` | — | one infinite 3D line |
| `concentric` | `[arcE, arcE]` | — | coincident centers |
| `coradial` | `[arcE, arcE]` | — | same center and radius |
| `on_plane` | `[pointH|lineE|arcE]` | `plane` | lies in the plane |
| `parallel_plane` | `[lineE]` | `plane` | direction ⟂ plane normal |
| `normal_plane` | `[lineE]` | `plane` | direction along the plane normal |
| `symmetric` | `[pointH, pointH]` | `plane` | mirror images about the plane |

`point_on` also accepts a spline (`[pointH, splineE]`). `plane` is `"front"` (XZ, normal −Y),
`"top"` (XY, +Z), `"right"` (YZ, +X) — the same frames as `eval_resolve_plane` — or
`{"origin":[x,y,z],"normal":[x,y,z]}`.

Spline evaluation: the 3D solver evaluates `spline` exactly like `GeomAPI_Interpolate`
(chord parametrisation; 2 points linear, 3 quadratic, ≥4 C2 cubic with Lagrange end
derivatives), so solved, compiled and displayed curves agree.

**Skipped report:** `Sketch3DSolution::skipped` lists `(constraint id, reason)` for every
constraint the solver could not apply; health/solve results surface it as
`skipped_constraints: [{id, type, reason}]`.

**GUI vertex references:** while a sketch3d is edited, "Reference Model Vertex" picks a solid
vertex, resolves its selector (`doc_resolve_pick`) and appends `{id:"x<N>", source:{kind:"vertex",
selector}}` to `params.external_refs` through `edit_feature` (§1b). The `x<N>` point is drawn as a
labelled diamond and is a valid point handle for coincident / distance / point_on / midpoint /
symmetric. Edge/face sources remain gated (§7 item 7).
