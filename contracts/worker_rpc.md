# Contract: Worker RPC — protocol 2

> Wire format is unchanged from today: newline-delimited JSON over stdio.
> Request `{"id": n, "method": str, "params": {…}}` →
> zero or more event lines `{"id": n, "event": kind, "data": {…}}` →
> final `{"id": n, "ok": true, "result": {…}}` or `{"id": n, "ok": false, "error": {code, message, detail}}`.
> (Legacy methods that returned `error` as a plain string keep doing so; all NEW methods use the
> structured error envelope of doc_ops.md §5.)
>
> **C35-overhaul (additive):** the ops-response gains `owner_map`; `doc_tree` now carries it too;
> sketch responses gain `constraints`; per-feature rebuild status gains `rolled_back`; new
> `doc_checkpoint`/`doc_revert` methods. All additions are append-only — existing fields unchanged.

## 1. Handshake & versioning

`ping` → `{"pong": true, "build123d": "...", "protocol": 2}`.
The host refuses to run against a worker whose `protocol` major differs from what it was built
for, and shows the repair-runtime flow. Workers tolerate unknown OPTIONAL params (ignore).

## 2. Doc worker (`core/api/libchudware.cpp`) — method table

### Legacy (unchanged, frozen)
`ping` · `run_model {code, name?}` · `get_prompts` · `shutdown`

> `agent_design` was REMOVED 2026-07-22 (T-RF5): the C++ `agent_run_v2` covers its
> surface; the method now returns "unknown method" everywhere.

### Documents
| method | params → result |
|---|---|
| `doc_new` | `{kind: "part"\|"assembly"}` → ops-response (doc_ops §1) + `doc_id` |
| `doc_open` | `{path}` → ops-response + `doc_id` + `warnings:[…]` |
| `doc_save` | `{doc_id, path?}` → `{path}` (`.step+` writer; path omitted = current) |
| `doc_close` | `{doc_id}` → `{closed: true}` (discards unsaved state) |
| `doc_list` | `{}` → `{docs:[{doc_id, name, kind, path, revision, dirty, feature_count, component_count, empty}]}` — `empty` is true when the doc has no authored content (part: zero features; assembly: zero features+components+mates). Appended 2026-07-16 (empty recovery auto-discard) |
| `doc_apply_ops` | doc_ops §1 envelope → ops-response (incl. `owner_map`) |
| `doc_undo` / `doc_redo` | `{doc_id}` → ops-response |
| `doc_tree` | `{doc_id}` → `{revision, tree, owner_map}` (no rebuild; uses the last build) |
| `doc_rebuild` | `{doc_id, from?: feature_id}` → ops-response (forced) |
| `doc_activate` | `{doc_id}` → ops-response — cheap activation for viewers/tab-switch: serves the engine's warm read-cache compile (same response shape as `doc_rebuild`, incl. a freshly published `tess.aicm_path`) **without** recompiling or re-tessellating when the doc is unchanged since its last build; falls back to the forced-rebuild path internally when that cache is cold. Read-only (never bumps the revision); allowed for ro attaches. Appended 2026-07-15 (viewport tab-switch perf) |
| `doc_resolve_pick` | `{doc_id, revision, body_id, kind: "face"\|"edge"\|"vertex"\|"body", topo_id}` → `{selector}` (errors `stale_revision` if revision moved) |
| `doc_checkpoint` | `{doc_id}` → `{checkpoint: "<id>"}` — deep-snapshot the current doc (PropertyManager-cancel). Multiple live checkpoints allowed; all cleared on doc close/new/open |
| `doc_revert` | `{doc_id, checkpoint}` → ops-response — restore the snapshot, rebuild, bump revision, and push ONE undo entry (a single `doc_undo` after a revert returns to the pre-revert state). Reverting when the doc already equals the snapshot is a cheap no-op but still returns the full response. `invalid_op` on an unknown checkpoint id |
| `doc_push_pull` | `{doc_id, body_id, topo_id, distance, commit?}` → push-pull response (Instant3D, §2.1) — drag a planar/cylindrical FACE by a signed `distance` (mm along the face outward normal); resolves the face's owning feature + the single drivable param, edits it, rebuilds. `commit:false` (default) = transient preview (no undo entry); `commit:true` = ONE undo entry. A face mapping to no single param returns `{ok:true, draggable:false, reason}` with NO doc mutation |

**`owner_map` (ops-response + `doc_tree`, C35-overhaul).** The compiler's per-feature topology
ownership, keyed by the feature id that created each face/edge. Ids are the **integer pick ids** the
AICM file and `doc_resolve_pick` use (body ordinal + 1-based topo index):

```json
"owner_map": {
  "f1": {"bodies": [1], "faces": [[1, 4], [1, 7]], "edges": [[1, 1], [1, 2]]},
  "f2": {"bodies": [1], "faces": [[1, 1], [1, 2], [1, 3]], "edges": [[1, 9]]}
}
```

`faces`/`edges` entries are `[body_id, topo_id]`; `bodies` lists body ids the feature owns (a feature
owns a body if it owns ≥1 of its faces). Empty for assemblies (no part-feature topology diff). This
is the same diff-tag heuristic the selectors contract consumes — booleans/fillets regenerate
coincident faces that may be re-attributed to the triggering feature (selectors.md §2).

**`rolled_back` (rebuild `feature_status`, C35-overhaul).** Each per-feature status row gains
`rolled_back: bool`. A feature **after** the rollback bar (doc `rollback_after`) is not compiled and
reports `{ok: true, rolled_back: true, error: null}` — distinct from an error (no error icon); the
geometry reflects only the prefix through the bar.

#### 2.1 `doc_push_pull` (Instant3D — drag a face to change the model)

Request `{doc_id, body_id, topo_id, distance, commit?}`:
- `body_id` — 1-based body ordinal (the host PickHit / AICM body id).
- `topo_id` — 1-based TopExp **face** index (same index `doc_resolve_pick` uses for `kind:"face"`).
- `distance` — mm, **signed along the face's outward normal**. Positive grows the body (push out),
  negative shrinks it (pull in).
- `commit` — `false` (default) = transient preview, pushes NOTHING onto the undo stack; `true` =
  pushes EXACTLY ONE undo entry (a single `doc_undo` after release returns to the pre-drag state).

Response (draggable face) — the full ops-response (`revision`/`id_map`/`tree`/`rebuild`/`tess`/
`owner_map`, so the host viewport updates exactly like a normal edit) PLUS:

```json
{"ok": true, "draggable": true,
 "feature_id": "f1", "feature_type": "box", "param": "height", "axis": "z",
 "old_value": 20.0, "new_value": 25.0, "distance": 5.0,
 "clamped": false, "committed": true, "revision": 3, "tess": {…}, …}
```

Response (non-draggable face — **no doc mutation**): `{ok:true, draggable:false, reason, feature_id?,
feature_type?}`.

**Face → drivable-param map (implemented).** The face is resolved to a `TopoInfo` (surf/axis/
centroid) and its owning feature via `owner_map`:

| owner feature | face | param | direction |
|---|---|---|---|
| `box` | planar, normal ∥ a principal axis | `length`/`width`/`height` (by axis) | `+= distance` |
| `extrude` (`end:"blind"`) | planar **cap** (normal ∥ extrude/sketch axis) | `depth` | `+= distance` (both caps grow on outward drag) |
| `extrude` | planar **side wall** (normal ⊥ extrude axis) | — | **draggable:false** |
| `extrude` (`end` != `blind`) | any | — | **draggable:false** (no single blind-depth param) |
| `cylinder` | cylindrical wall | `radius` | `+= distance` (radial) |
| `cylinder`/`cone` | planar cap | `height` | `+= distance` (axial) |
| `hole` (`standard:"custom"`) | cylindrical bore | `diameter` | `+= 2·distance` (radial doubles the diameter) |
| `hole` (standard table) | bore | — | **draggable:false** (bore driven by the standard) |
| offset/midplane `plane` datum | the moved plane | `offset` | `+= distance` |
| `revolve` / `sphere` / `cone` round wall / sweep / loft / bspline | any | — | **draggable:false** (angle-/profile-driven; no single linear param) |
| any param that is **expression-driven** (`expr`) | — | — | **draggable:false** (edit the equation) |

New length/radius/depth/diameter values clamp to a `0.1 mm` floor (`clamped:true`) so a drag can
never invert or collapse the body.

**Transient-preview vs commit (host envelope).** For a live drag the host: (1) `doc_checkpoint`
at drag start; (2) per drag sample, `doc_revert` to the checkpoint then
`doc_push_pull(commit:false)` for the CURRENT cumulative distance — so previews never compound and
a cancel is a single `doc_revert`; (3) on release, `doc_revert` to the checkpoint then ONE
`doc_push_pull(commit:true)`, which is the single undoable step. A degenerate/failed rebuild rolls
the document fully back and returns `draggable:false` with a reason (never corrupts the doc).

#### 2.2 Configurations (CONFIG — `config_*`)

Named configurations: variants of one part/assembly driven by per-config feature-param overrides +
feature/component suppression (doc_schema.md §9). All `config_*` methods take `{doc_id, …}` and use
the structured error envelope. `config_list` is read-only; every other `config_*` method is a single
**undoable** document edit that bumps the revision and returns the full **ops-response**
(`revision`/`id_map`/`tree`/`rebuild`/`tess`/`owner_map`). Activating a config — or editing the
**active** config — rebuilds; editing an **inactive** config is a metadata-only write that reuses the
last build but still returns the ops-response so the host refreshes its config table.

| method | params → result |
|---|---|
| `config_list` | `{doc_id}` → `{configurations:[name…], active, detail:[{name, param_overrides, suppressions}]}` (read-only, no rebuild). A config-less doc reports the single implicit `["Default"]`, `active:"Default"` |
| `config_add` | `{doc_id, name, copy_from?}` → ops-response. Adds a config (optionally seeding overrides/suppressions from `copy_from`). Does NOT activate it (reuses the last build). `invalid_op` on a duplicate name |
| `config_delete` | `{doc_id, name}` → ops-response. Deletes a config; deleting the ACTIVE one falls the active pointer back to the first remaining config and rebuilds. The LAST config cannot be deleted |
| `config_activate` | `{doc_id, name}` → ops-response for the NEW active config (tree/rebuild/tess reflect its overrides + suppression). `invalid_op` on an unknown name |
| `config_set_override` | `{doc_id, name, feature_id, param, value}` → ops-response. Sets a per-config override of `feature_id.param`; `value` is a scalar or a `{value, expr}` dim. `value: null` (or `clear: true`) CLEARS the override (the base param value is used) |
| `config_set_suppression` | `{doc_id, name, feature_id, suppressed}` → ops-response. Sets a per-config suppression of `feature_id` (a `k<N>` component id in assemblies). `suppressed: false` drops the entry (configs stay minimal) |

The active configuration is surfaced on the ops-response at `rebuild.doc_status.active_configuration`
(the config the returned geometry reflects; `"Default"` for config-less docs). A `config_*` edit that
would produce an invalid document (e.g. an override/suppression naming a non-existent feature) is
rejected with `invalid_op` and the session document is left **unchanged**. A config that fails to
compile reports per-feature errors in `rebuild.feature_status` exactly like a normal rebuild.

Implemented by `core/api/libchudware.cpp` (`config_*`) over `core/doc/session.cpp`
(`config_list`/`config_add`/`config_delete`/`config_activate`/`config_set_override`/
`config_set_suppression`).

#### 2.3 Weldments cut list (`weld_cut_list`)

The signature weldment deliverable: aggregate every structural member produced by the part's
`weld_member` features (feature_params.md §Weldments) into a grouped cut-list table. **Read-only** —
it never mutates the document or bumps the revision. Structured error envelope.

| method | params → result |
|---|---|
| `weld_cut_list` | `{doc_id}` → `{rows:[{item, profile, description, length_mm, quantity, total_length_mm}], total_members, total_length_mm, profiles:[…]}` (read-only). A part with NO `weld_member` features (or an assembly) reports `rows:[]` with zero totals — **not** an error |

Members across ALL `weld_member` features are pooled and grouped by `(profile + resolved dims +
length rounded to 0.1 mm)`: identical members collapse into ONE row carrying `quantity` (the count)
and `total_length_mm` (= `quantity × length_mm`); distinct lengths get separate rows. `description`
is a human profile label (e.g. `"Square Tube 20 x 20 x 2"`). Rows sort by `profile` (asc) then
`length_mm` (desc); `item` is the 1-based post-sort row number. `total_length_mm` is the sum of all
member lengths; `profiles` lists the distinct profile names present.

```json
{
  "rows": [
    {"item": 1, "profile": "square_tube", "description": "Square Tube 20 x 20 x 2",
     "length_mm": 100.0, "quantity": 2, "total_length_mm": 200.0},
    {"item": 2, "profile": "square_tube", "description": "Square Tube 20 x 20 x 2",
     "length_mm": 80.0, "quantity": 1, "total_length_mm": 80.0}
  ],
  "total_members": 3, "total_length_mm": 280.0, "profiles": ["square_tube"]
}
```

Implemented by `core/api/libchudware.cpp` (`weld_cut_list`) → `core/doc/session.cpp`
(`cut_list_for_session` forces a fresh uncached compile so the `weld_member` compiler repopulates
the `_weld_members` side-channel surfaced on `CompileResult.weld_members`, then `build_cut_list`
groups it). Host passthrough:
`DocMirror::weldCutList` + the "Cut List" panel.

### Sketch interaction (M2)
| method | params → result |
|---|---|
| `sketch_enter` | `{doc_id, sketch}` → `{entities:[{id, …solved params}], statuses:{e1:"full",…}, dof:int, plane_frame:[16], conflicting:[cid…], redundant:[cid…], solve_rejected:bool, status, constraints:[…]}` |
| `sketch_solve` | `{doc_id, sketch}` → same as `sketch_enter` |
| `sketch_probe` | `{doc_id, sketch, action}` → over-define pre-flight (read-only, see below) |
| `sketch_drag` | `{doc_id, sketch, handle, xy:[x,y]}` OR `{doc_id, sketch, targets:[{handle, xy:[x,y]},…]}` → `{entities, statuses, solve_rejected:false, constraints}` — transient, warm-started, does NOT commit or bump revision. `targets` (optional) soft-targets many point handles at once for whole-entity / closed-profile / multi-select drags; each point translates while constraints stay exact. When `targets` is present `handle`/`xy` are ignored. |
| `sketch_drag_commit` | `{doc_id, sketch}` → ops-response (persists final positions as one undoable batch) + `constraints` |
| `sketch_exit` | `{doc_id, sketch}` → ops-response (rebuilds downstream) |

### 3D sketch interaction (`sketch3d` — SKETCH3D migration)
| method | params → result |
|---|---|
| `sketch3d_health` | `{doc_id, sketch}` → `{ok, status:"under"\|"full"\|"over", dof, converged, statuses:{eid…}, redundant:[cid…], conflicting:[cid…], free:[{entity,param,var}…], entities:[{id, type, construction, …solved world coords}], wires:[{closed, n_edges}]}` — stateless solve + wire-chain report (wire order == the sweep compiler's spine/guide index). |
| `sketch3d_drag` | `{doc_id, sketch, handle, xyz:[x,y,z]}` OR `{doc_id, sketch, targets:[{handle, xyz:[x,y,z]},…]}` → `{ok, entities, statuses, rejected_handles, converged, budget_exceeded}` — transient constrained drag; does NOT commit. Commit = `doc_apply_ops` `edit_feature` with the solved `entities` array (no dedicated commit RPC). Full contract + examples: [sketch3d_gui_contract.md](sketch3d_gui_contract.md). |

**`solve_rejected` (sketch_enter / sketch_solve / sketch_drag, C50f).** Conflict-safe solving:
when a re-solve comes back **contradictory** (`conflicting` non-empty) OR **non-convergent**, the
solver's least-squares compromise is rejected for geometry. The response's `entities` carry the
document's last-valid (pre-solve) coords instead — geometry is **frozen**, never persisted, and the
warm state is dropped — while `dof`/`statuses`/`conflicting`/`redundant`/`status` still report the
real over-defined analysis so the host can show "Over Defined" and which constraints conflict.
`solve_rejected:true` flags this freeze so the host can message the user. Consistent
over-definition (`redundant` non-empty, `conflicting` empty, convergent) is **not** rejected — it
has a valid solution and solves/persists as before (`solve_rejected:false`).

**`sketch_probe` (C50f over-define pre-flight).** A dry run over a **deep copy** of the active
sketch — it MUST NOT mutate the document, session sketch state, undo stack, or warm-start state.
`action` is exactly one of:

```json
{"add_constraint": {<constraint json, same schema as sketch_add_constraint's constraint>}}
{"set_dim": {"id": "<cid>", "value": <num>, "expr"?: "<str>", "driven"?: <bool>}}
{"delete_constraint": {"id": "<cid>"}}
```

Response:

```json
{"ok": true, "status": "under"|"full"|"over",
 "dof_before": int, "dof_after": int, "converged": bool,
 "conflicting": ["<cid>", …], "redundant": ["<cid>", …],
 "would_over_define": bool}
```

The probed `add_constraint` is tagged id `__probe__` so it can be spotted in `conflicting`/
`redundant`. `would_over_define` is true when the probe constraint appears in `conflicting` or
`redundant`, OR a **dimensional** add leaves `dof_after >= dof_before` (absorbed by an already
determined system), OR the solve no longer converges (e.g. a `set_dim` to a contradictory value),
OR the system is otherwise conflicting. On an unmatched/inactive sketch or malformed action:
`{"ok": false, "error": "<reason>"}`.

**`constraints` (sketch responses, C35-overhaul).** Every constraint in the active sketch, for the
sketcher's dimension annotations. Each record:

```json
{"id": "c5", "type": "distance", "refs": ["e1.p1", "e1.p2"],
 "value": 80.0, "expr": null, "driven": false,
 "placement": [40.0, -5.0], "measured": 80.0}
```

`value`/`expr` come from the stored dim (`null` for geometric relations). `driven` defaults false,
`placement` is the `[u,v]` annotation anchor (`null` if unset). `measured` is the **current solved
measurement** for dimensional types (distance/angle in mm/deg, radius/diameter in mm; angle in
`[0,180)`); `null` for geometric relations (coincident/parallel/…) and when refs cannot resolve.
Driven (reference) dims still report `measured`.

### Drawings (W12 — 2D orthographic/iso views of a part/assembly via HLR)

A drawing document (`aicad.drawing/1`, doc_schema.md §8) projects a SOURCE part/assembly into 2D
views. These methods live in `core/doc/drawing.cpp` (registered via `drawing.WORKER_METHODS`) and
keep a SEPARATE registry from `doc_*` sessions — a `drawing_*` `doc_id` is a drawing id, not a part
session id. All use the **structured error envelope**. The projected geometry is recomputed from the
source's CURRENT bodies on every fetch (associative); the host re-fetches after editing the source.

| method | params → result |
|---|---|
| `drawing_new` | `{source_doc_id, name?}` → `{doc_id, drawing}` — create a drawing referencing an OPEN source part/assembly session. `drawing` is the doc JSON (one default A3 sheet `s1`, no views). `unknown_doc` if the source isn't an open session. |
| `drawing_add_view` | `{doc_id, type, orientation, sheet_id?, scale?, position?, show_hidden?}` → `{view_id, revision, view}` — append a view (defaults: `sheet_id`=first sheet, `scale`=1.0, `position`=[0,0], `show_hidden`=true; an `iso` type forces `orientation:"iso"`). `view` is the projected 2D geometry for the new view (shape below). |
| `drawing_set_view` | `{doc_id, view_id, scale?, position?, orientation?, show_hidden?}` → `{view_id, revision, view}` — move/rescale/re-orient (orientation ignored for `iso` views); returns the re-projected `view`. |
| `drawing_remove_view` | `{doc_id, view_id}` → `{removed: true, revision}`. `invalid_op` on an unknown view. |
| `drawing_get` | `{doc_id}` → full drawing + every view's projected geometry (shape below). |
| `drawing_close` | `{doc_id}` → `{closed: bool}`. |

**`drawing_get` result.** Re-projects ALL views against the source's current bodies:

```json
{
  "doc_id": "…", "revision": 5,
  "source_doc_id": "…", "source_revision": 12,
  "tree": { <the DrawingDocument JSON> },
  "total_hlr_ms": 6.1,
  "sheets": [
    {"id": "s1", "size": "A3", "size_mm": [420.0, 297.0],
     "views": [ <ViewGeometry>, … ]}
  ]
}
```

**`ViewGeometry` — the normative 2D view-geometry shape** (returned per view by `drawing_get`,
`drawing_add_view`, `drawing_set_view`). A polyline is a list of `[x, y]` points in **sheet mm**
(already scaled by the view `scale` and translated to the view `position`):

```json
{
  "view_id": "v1", "type": "base", "orientation": "front",
  "scale": 1.0, "position": [100.0, 100.0], "show_hidden": true,
  "visible": [ [[x,y],[x,y],…], … ],   // solid lines: sharp edges + silhouette outline + smooth/tangent
  "hidden":  [ [[x,y],[x,y],…], … ],   // dashed lines: hidden sharp edges (empty when show_hidden=false)
  "bbox": [min_x, min_y, max_x, max_y], // sheet mm, or null when the view has no geometry
  "ms": 2.0,                             // HLR timing for this view
  "error": null                          // string when the projection failed (then visible/hidden empty)
}
```

- Straight model edges become 2-point polylines; curved edges (circles, fillets, splines) are
  discretized (≈0.05 mm chord / 0.35 rad deflection) into many-point polylines.
- `visible` merges OCCT's `VCompound` (sharp), `OutLineVCompound` (silhouette) and
  `Rg1LineVCompound` (smooth/tangent, e.g. a fillet run-out). `hidden` merges `HCompound` +
  `OutLineHCompound`. Orientation→direction is normative in doc_schema.md §8 (Z-up: front=+Y).
- **Containment:** a single view's HLR/OCCT failure sets `error` and empties that view's geometry;
  the rest of the drawing is unaffected and the call still succeeds. HLR is the slow step (a few ms
  per simple solid; tens-of-ms on complex parts); results are cached per
  `(source_revision, orientation, scale, show_hidden)` so re-fetching an unchanged source is cheap
  (position is re-applied as a cheap translate without re-running HLR).

#### Section + Detail views (W15 — additive)

Two DERIVED view types built on the same HLR projection, each anchored to a `parent` view id.

| method | params → result |
|---|---|
| `drawing_add_section` | `{doc_id, parent_view_id, plane?|cut_line?, label?, scale?, position?, sheet_id?, show_hidden?}` → `{view_id, revision, view}` — cut the model and project the cut. The cut is EITHER a world `plane` `{normal:[x,y,z], point:[x,y,z]}` OR a 2D `cut_line` `[[x1,y1],[x2,y2]]` in the PARENT view's sheet-mm coords (a cutting line dropped across the parent). Defaults: `label`=next free letter, `scale`=parent's, `show_hidden`=false. `view` is a ViewGeometry with `type:"section"`, `orientation:"section"`, `parent`, `label`, the cutting definition echoed back, and a new **`section_faces`** field (below). `invalid_op` on an unknown parent or a missing/degenerate cut definition. |
| `drawing_add_detail` | `{doc_id, parent_view_id, center, radius, scale?, label?, position?, sheet_id?, show_hidden?}` → `{view_id, revision, view}` — a magnified circular crop of the parent. `center` is `[x,y]` in the PARENT's sheet mm; `radius` > 0 (parent sheet mm). Defaults: `label`=next free letter, `scale`=2× the parent's. `view` is a ViewGeometry with `type:"detail"`, `orientation:"detail"`, `parent`, `label`, `center`, `radius`. `invalid_op` on an unknown parent, a non-`[x,y]` center, or a non-positive radius. |

**`section_faces` (section ViewGeometry, W15).** A list of CLOSED polygons (each `[[x,y],…]` in
sheet mm, ≥3 points) — the cross-section areas where the cut plane slices solid material. The host
HATCHES these (45° lines clipped to each polygon). A through hole / cavity SPLITS the cut face into
several polygons, so the bore reads as a gap in the hatch (the section reveals the bore). The
section's HLR is run on the CUT solid (the half between the eye and the plane is removed via an
OCCT `BRepAlgoAPI_Cut` against a large half-space box), so the visible polylines show the actual cut
silhouette + the geometry behind it. A cut plane that misses the model sets `error` +
empty `section_faces` (contained, not a raise).

**Parent markers (attached by `drawing_get`).** Each PARENT view's geometry gains, when it has
children: `section_lines:[{label, view_id, line:[[x,y],[x,y]]}]` (the cutting line to draw across the
parent, sheet mm — only for `cut_line`-defined sections) and
`detail_circles:[{label, view_id, center:[x,y], radius}]` (the detail bubble). The host draws the
labelled cutting line / detail circle on the parent and titles each derived view
("SECTION A-A" / "DETAIL B").

- **Geometry model / limitation:** the section cut is a real OCCT half-space boolean on the source
  solids (real cut geometry, bore revealed, hatched cut faces), projected along the cut normal. The
  `cut_line` form un-projects the two parent sheet points through the parent's view frame to a world
  cutting plane that contains the cut line and the parent's view direction (the SolidWorks "drop a
  line, look sideways" model). Section faces are detected as planar faces of the cut solid that are
  coplanar with and lie on the cut plane. Section/detail views are NOT position-cached (each
  re-cuts / re-derives from the live parent on every fetch, staying associative). A cut line parallel
  to the parent's view direction, or a section whose plane contains no solid material, yields a
  contained `error`.

### Evaluate (M5; overlay-producing methods return `{aicm_path}` with `SCLR` chunks)
`eval_measure {doc_id, refs:[Selector,…]}` → `{kind, distance?, angle?, length?, area?, radius?,
points?:[…]} `· `eval_mass {doc_id, body?}` → `{volume_mm3, mass_g, com:[3], inertia:[9],
material}` · `eval_section_props {doc_id, plane: Selector|datum-id}` · `eval_interference
{doc_id}` → fit-report shape (kernel `_fit_report`) · `eval_draft {doc_id, pull:[3], angle_deg}`
· `eval_thickness {doc_id}` · `eval_curvature {doc_id, kind: "gaussian"\|"mean"\|"max"}` ·
`eval_check {doc_id}` → `{issues:[{kind, severity, text, ref?}]}` · `eval_symmetry {doc_id,
plane}` → `{symmetric: bool, deviation_mm3}` · `eval_perf {doc_id}` → `{features:[{id, name, ms}]}`

### Formats (M8; minimal `doc_save`/`doc_open` for `.step+` exist from M1)
`import_file {path, into?: doc_id}` → ops-response (adds `import` feature(s)) ·
`export_file {doc_id, path, format: "step"|"stl"|"brep"|"iges"|"3mf"}` → `{path}`

#### 2D export — drawing sheets + sheet-metal flat patterns (W14, DXF/PDF)

Write the 2D geometry the engine already produces to a manufacturing file. **DXF** is a native R12
(AC1009) ASCII file: a TABLES section declaring named layers (color + linetype) and an ENTITIES
section of canonical `POLYLINE`/`VERTEX`/`SEQEND` chains (opens in LibreCAD / AutoCAD / Fusion /
ezdxf). **PDF** is a single-page 1:1 mm vector page (hand-written PDF 1.4; no library dependency —
solid strokes for visible/outline, dashed for hidden/bend). Format comes from an explicit `format`
param else the path extension; structured error envelope; contained errors (bad path/format/no
geometry) never crash the worker. Implemented by `core/api/libchudware.cpp` → `core/doc/export2d.cpp`
(+ `core/doc/dxf.cpp`, `core/doc/pdfvec.cpp`).

| method | params → result |
|---|---|
| `export_drawing` | `{doc_id, path, format?:"dxf"\|"pdf"}` → `{path, format, visible, hidden, entities}`. `doc_id` is a **drawing** registry id (from `drawing_new`). Flattens every sheet/view's `visible`+`hidden` polylines (already in absolute sheet-mm) to layers **VISIBLE** (solid) + **HIDDEN** (dashed) and writes the file. `export_failed` when the drawing has no view geometry. |
| `export_flat_dxf` | `{doc_id, path, format?:"dxf"\|"pdf"}` → `{path, format, bend_lines, outline_points, flat_area, entities}`. `doc_id` is a **part** doc id. The active sheet-metal part's flat pattern: the blank boundary on layer **OUTLINE** + the bend lines on a SEPARATE layer **BEND** (the key sheet-metal split). Compiles with a FRESH cache so `sm_flat_pattern` repopulates its side-channel (same caveat as `weld_cut_list`); `invalid_op` when the part has no flat pattern (add a Flatten feature first) or is an assembly. |

Layer→style: VISIBLE/OUTLINE = white(7)/continuous, HIDDEN = red(1)/dashed, BEND = green(3)/
continuous. The flat-pattern geometry is surfaced on `CompileResult.sheet_metal_flat`
(`{fid: {bend_lines, outline, flat_area, n_base, …}}`) from the `sm_flat_pattern` compiler's
`ctx.datums["_sheet_metal_flat"]` side-channel, then projected to 2D using the stored `n_base`.

### AI (M9)
`agent_run_v2 {request, doc_id, options?:{max_stage_attempts?, plan?, roles_override?, workflow?}}`
→ streams events.md orchestrator events → `{run_id, status, stop_reason, revision}` ·
`get_prompts` (v2 result: `{prompts:[{role, path, override_path, text, overridden}]}`) ·
`agent_cancel {run_id}` → best-effort stop at the next stage boundary. ·
`agent_steer {run_id, text}` → `{ok, steered}`; queues a `[User steering]` user message the
running turn consumes at its next stage/attempt boundary (cooperative, like cancel — never
interrupts an in-flight provider HTTP call). Unknown/finished run_id → `{ok:true, steered:false}`.

### AI context references (Wave 4, append-only)
`agent_design` and `agent_run_v2` accept an OPTIONAL `context_refs?: [{kind, id, doc_id?,
label}]` array — model entities the user attached to the prompt via the composer context-cube
picker (kind ∈ `component|body|face|edge|sketch|feature`). The host ALSO inlines the same
refs as a plain-text "Context references:" block in `request` so the C++ `agent_run` path
surfaces them; the structured array is the machine-readable mirror for the Python turn path
(`loop.run_turn` / `agent_turn.run_turn`). Absent/empty = no-op; malformed entries skipped.

`options.workflow`/`options.profile`: `"auto"` (default), `"micro"`/`"local"`/`"small-context"`
to force the decomposed local-model workflow, or `"full"`/`"standard"` to force the normal v2
machine. The micro workflow decomposes into tiny single-step executor calls and applies/validates
after each step, which keeps limited local models auditable and recoverable.

`agent_ask_v2 {doc_id?, question}` → `{answer}`. Read-only help/QA against the current
document digest; unknown or empty `doc_id` falls back to general CAD help.

`agent_pm_assist_v1 {doc_id?, intent, form_schema, current, selected_refs?}` →
`{answer, patches, activate_well, reveal_fields, highlights, questions}`. Read-only
PropertyManager assist for `Select with AI`. The worker validates provider output against the
form schema before returning it. `patches` contains only ordinary currently visible fields
(`dim`, `combo`, `check`, `int`, `datum_ref`); topology refs are never invented. When a real pick
is needed, `activate_well` names a selector well for the host to focus after any patches make it
visible.

### Vendor parts
Compliance-friendly supplier CAD cache. These methods do not scrape catalogs; a user download or an
approved supplier API adapter supplies the CAD file, then the worker stores compact metadata under
the app data vendor cache for local AI workflows.

| method | params → result |
|---|---|
| `vendor_part_register_local_cad` | `{supplier, part_number, cad_path, description?, product_url?, specs?, source?, copy_file?}` → cached vendor part record. Supported import formats: STEP/STP, IGES/IGS, BREP, STL. |
| `vendor_part_list` | `{supplier?, query?}` → `{parts:[...]}` |
| `vendor_part_context` | `{limit?, query?}` → `{count, parts, llm_context, capabilities}` compact enough to inject into local-model prompts. |
| `vendor_part_insert` | `{supplier, part_number, doc_id?/into?, base_revision?, name?}` → normal doc ops response after adding an `import` feature with vendor metadata preserved in feature params. |

### Recognition & overlap (AI-experience wave — appended; see docs/review/GEOMETRY_LLM_REPRESENTATION_REVIEW.md)

| method | params → result |
|---|---|
| `eval_feature_overlap` | `{doc_id, features?: [feature-id,…], min_volume_mm3?: 1e-3}` → `{ok, summary, pairs:[{a, b, a_name?, b_name?, kind: "boss_overlap"\|"cut_overlap"\|"cut_into_boss"\|"boss_swallowed", volume_mm3}], dead_features:[feature-id,…]}`. Pairwise common volume of the per-feature TOOL solids captured at compile; `features` restricts to pairs touching those ids (add_features checks only its new batch). `ok:false` only for `boss_swallowed` (a cut removed an ENTIRE earlier boss); every other kind is a warning. `dead_features` = ok features owning zero surviving faces. |
| `eval_compare` | `{doc_a, doc_b, body_a?, body_b?}` → `{ok, match_pct, volume_a_mm3, volume_b_mm3, common_mm3, missing_mm3, extra_mm3, residual_regions:[{side: "missing"\|"extra", volume_mm3, center:[3], size:[3]}], summary}` or degraded `{ok, match_pct: null, degraded: true, volume/bbox/centroid agreement fields, summary}` when the boolean fails. Shapes compared in their own world frames (no registration). Default body = fuse of all solid bodies per doc. |
| `doc_face_wires` | `{doc_id, face: Selector\|sid}` → `{ok, plane_frame, entities:[sketch-entity JSON, doc_schema §3.1], wires:[{outer: bool, entity_ids:[…]}], approx: bool}`. Convert-entities primitive: a PLANAR face's boundary wires as sketch entities in the face's 2D plane frame (lines/circles/arcs/ellipses exact; other curves sampled as polylines with `approx:true`). Non-planar face → `invalid_op`. |

`eval_interference` accepts two appended optional params: `include_coincident?: bool = false`
(adds `contacts:[{a, b, distance}]` for touching, non-overlapping pairs) and
`min_clearance_mm?: number = 0` (clearance entries below the threshold carry
`warning: "below min clearance"`). Return shape otherwise unchanged.

### Copilot voice input / OMP ASR sidecar (append-only, 2026-07-27)

Proxied by `libchudware` → bun OMP bridge (`vendor/omp/bridge/src/stt`). Reuses OMP's
`pi-natives` `AudioCapture` + Whisper (`@huggingface/transformers`) approach. When the
ASR runtime or mic native addon is missing, `available` is false and the GUI disables
the mic with a tooltip hint — never crashes.

| method | params → result |
|---|---|
| `agent_stt_status` | `{}` → `{ok, available, reason?, state: "idle"\|"listening"\|"processing"\|"error", model?, modelLabel?, modelSizeHint?, modelCached?, partial?, committed?, error?, level?}` |
| `agent_stt_start` | `{model?: "fast"\|"balanced"\|"turbo", language?}` → same shape as status (`state:"listening"` on success). First use may download the Whisper ONNX weights (~60 MB for `fast`). |
| `agent_stt_stop` | `{}` → status fields + `{text}` final transcript (committed segments joined). |
| `agent_stt_cancel` | `{}` → status with `state:"idle"`; discards partial/committed audio without inserting text. |

Env: `CHUDWARE_STT_MODEL` overrides the default model key (`fast`).

## 3. Sim worker (`core/api/libchudware.cpp`, separate process, spawned on demand)

Same wire protocol. One job in flight; `job_id` correlates events. This table was re-verified
against the native and Python implementations on 2026-08-15.

| method | params → result |
|---|---|
| `ping` | → `{pong: true, protocol: 2, sim: true}` |
| `sim_mesh` | `{step_path, fineness?: 0..1, density?: 0..1 (legacy alias), order?\|mesh_order?, curvature?, curv_elems_per_circle?, target_size_mm?, min_size_mm?, max_size_mm?, optimize?, mesher_backend?: "netgen"\|"polymesh" (default "netgen"), structured?\|mesh_type?: "hex", auto_type?, quad_surface?, fallback_to_tet?, thin_wall_layers?}` → `{ok, points:[…], tets:[…], char_len_mm, n_nodes, n_elems, nodes_per_elem, min_tet_volume_mm3, max_tet_edge_ratio, aspect_min, aspect_avg, aspect_max, jacobian_min, pct_poor, aspect_hist:[…], mesh_type_used, note, mesher_backend}`. There is no `mesh_path`. Requesting `"polymesh"` when the build lacks `CHUDWARE_WITH_POLYMESH` returns `{ok:false,error}`. |
| `sim_run_static` | `{study}` where `study` carries `step_path` or inline `points`/`tets`, material, fixtures, loads, mesh options, and optional `mesher_backend: "netgen"\|"polymesh"` → `{ok, displacement:[…], disp_magnitude:[…], von_mises:[…], principal1:[…], principal3:[…], max_shear:[…], stress_xx:[…], stress_yy:[…], stress_zz:[…], n_dof, n_nodes, n_elems, mesh_type_used, summary:{max_von_mises_mpa,max_disp_mm,min_fos,max_principal_mpa,min_principal_mpa,max_shear_mpa,min_fos_tresca,min_fos_rankine,max_tresca_mpa,max_abs_principal_mpa}, solver_notes:[…], cache_hit, cache_reason, input_hash, overlay_aicm?}`. `overlay_aicm` is best-effort for STEP-backed solves; there is no `results_npz`. |
| `sim_run_modal` | `{study, n_modes?: 6}`; `study.mesher_backend` accepts `"netgen"` (default) or `"polymesh"` → `{ok, freqs_hz:[…], mode_shapes:[…], n_modes, n_nodes, n_dof, mesh_type_used, solver_notes:[…], cache_hit, cache_reason, input_hash, overlay_aicm?}`. |
| `sim_run_thermal` | `{study}` (steady heat conduction; `study.mesher_backend` accepts `"netgen"` default or `"polymesh"`) → `{ok, temperature:[…], n_nodes, n_dof, n_elems, mesh_type_used, summary:{max_temp_c,min_temp_c}, flux_summary:[…], solver_notes:[…], cache_hit, cache_reason, input_hash}`. |
| `sim_run_fatigue` | `{study}` (high-cycle fatigue over a static solve; `study.mesher_backend` accepts `"netgen"` default or `"polymesh"`) → `{ok, n_nodes, n_elems, n_dof, summary:{se_corrected_mpa,se_base_mpa,sut_mpa,sy_mpa,lmin,lmax,crit_sigma_a,crit_sigma_m,theories:{goodman,gerber,soderberg,asme,morrow}}, solver_notes:[…], overlay_aicm?}`. |
| `sim_run_buckling` | `{L_mm?, b_mm?, h_mm?, E_mpa?, nu?, load_n?, n_modes?: 4}` → `{ok, backend, solver, applied_load_n, load_factors:[…], critical_loads_n:[…], euler_reference_pcr_n, note}`; returns an honest skipped/error payload when CalculiX is unavailable or the setup/solve fails. |
| `sim_run_contact` | `{L_mm?, b_mm?, h_mm?, E_mpa?, nu?, pressure_mpa?}` → `{ok, backend, solver, applied_load_n, base_reaction_n, reaction_balance_error_pct, note}`; fails if CalculiX is unavailable or force balance exceeds 5%. |
| `sim_freq_response` | `{freqs_hz, mode_shapes, n_nodes, total_mass_kg, excitation_dir?:[3], zeta?:0.02, base_accel_g?:1, sweep_hz?:[…], psd_breakpoints?:[[f_hz,g2_per_hz],…], vibration_spec?:{…}}` → `{ok, freqs_hz, harmonic, random, verdict, damping_assumption}`. |
| `sim_caps` | `{}` → `{ok, ccx:{found,path,hint}, freq_response:{available,hint}, handoffs:{ecad_thermal,heatsink_select,motion_fea,piping_verify_cfd}}`. |
| `sim_lbm` | `{mask:{nx,ny,nz,solid:[…]}, params:{u_lat?,tau0?,voxel_mm?,dx_m?,dt_s?,rho_air?,backend?}, n_steps, backend?}` → `{ok, nx, ny, nz, ux:[…], uy:[…], uz:[…], rho:[…], cd, drag_n, lift_n, frontal_area_mm2, steps_run, diverged, backend, backend_fallback}`. |
| `sim_run_flow` | STL form `{stl_path, direction?:[3], speed_mps?, preset?: "coarse"\|"medium"\|"fine"\|"xfine", solver?:"lbm", aoa_deg?, sideslip_deg?, roughness_um?, ref_area_mm2?, fluid?, cells?, n_steps?, progress_path?, job_id?}` → `{ok, drag_n, lift_n, side_n, cd, cl, ld, reynolds, frontal_area_mm2, ref_area_mm2, reynolds_note, diverged, trace:[], cp_min, cp_max, vmax_mps, solver_notes, scalar_fields:[], overlay_pending:true, steps_run, converged, residual, mass_imbalance, voxel_mm, min_feature_mm, min_feature_voxels, grid, warning?}`. Document form `{doc_id, velocity?, aoa?, fluid?, cells?, n_steps?, progress_path?, job_id?}` returns the corresponding force/convergence subset. |
| `sim_cancel` | `{job_id}` → `{cancelled: bool}` (idempotent; unknown id is `false`). |
| `sim_study_list` | `{doc_id}` → `{studies:[…], revision}`. |
| `sim_study_add` | `{doc_id, study}` → `{studies:[…], revision, study_id}`; the core assigns an id when absent. |
| `sim_study_edit` | `{doc_id, study_id, study}` → `{studies:[…], revision}`; the addressed id is preserved. |
| `sim_study_delete` | `{doc_id, study_id}` → `{studies:[…], revision}`. |
| `shutdown` | |

All `sim_run_*`/`sim_mesh` stream `sim_progress` events (events.md). The sim worker receives
geometry as FILES (STEP/STL paths) + study JSON — it never touches live document state, so a
crash cannot corrupt a session (host restarts it on demand).

## 4. Concurrency & timeouts

- Doc worker: requests are serialized by the host (existing mutex). Doc/sketch calls must stay
  interactive (<100 ms typical; rebuilds stream `doc_progress` past 300 ms).
- Sim worker: one job; host may poll `ping` concurrently is NOT supported — host tracks job state
  from events. Host-side timeout: none for sim (cancel is explicit), 120 s default elsewhere.
- The host owns process lifecycle: doc worker at startup (existing flow), sim worker lazily,
  restart on crash with a console notice.

## 5. Multi-user identity & history (append-only, 2026-07-16)

### 5.1 `hello` user param (hostd)

Daemon clients may declare seat identity on the first `hello` handshake:

```json
{"method":"hello","params":{
  "client":"gui"|"mcp"|"agent"|"test"|"control",
  "pid": 1234,
  "proto": 1,
  "user": {"id": "alice@example.com", "display": "Alice"}   // optional
}}
```

- Wire field for client kind remains `client` (values gui|mcp|agent|test|control), **not** `kind`.
- `user` is optional. When present, both `id` and `display` are strings (empty string allowed).
- The daemon stores `{id,display}` on the per-connection state and stamps them onto subsequent
  `doc.changed` events as `data.user`. Local single-seat GUIs omit `user` and events carry empty
  strings.

### 5.2 `doc_clear_history`

| method | params → result |
|---|---|
| `doc_clear_history` | `{doc_id}` → `{ok:true, doc_id}` — clears that doc's undo_stack + redo_stack and wipes its on-disk undo journal. **No revision bump.** Invoked automatically by hostd after a successful `doc_save` from a **gui**-kind client; agent/mcp saves do **not** clear. |

### 5.3 Agent undo gating

`doc_undo` / `doc_redo` from a connection whose `client` kind is `mcp` or `agent` are rejected with:

```json
{"code":"undo_denied","message":"agents use doc_checkpoint/doc_revert or PLM rollback"}
```

unless env `CHUDWARE_UNDO_AGENTS=1` is set (kill-switch restoring free undo for agents). Agents keep
`doc_checkpoint` / `doc_revert` and PLM rollback for intentional rollback.


## 6. Sub-agent control plane (append-only, 2026-08-03 — T-DP16)

Worker methods served by `services/agent/subagent_rpc.py` over the file-backed registry
`services/agent/subagent_registry.py`. Every control is **advisory and turn-boundary bound**:
nothing interrupts in-flight work, and each response carries an honest `note` saying so.
Enforcement lives in exactly one place — the `if subagent_id:` block at the top of
`agent_turn.run_turn`'s completion loop.

| method | params → result |
|---|---|
| `subagent_list` | `{session_id?: string}` → `{ok, sessions[]}` — a non-empty `session_id` returns only sub-agents owned by that conversation |
| `subagent_transcript` | `{subagent_id, raw?: bool}` → `{ok, session, messages[], pending_steers[]}` — `raw` defaults to `false`; when `true`, `messages` contains the lossless raw-log records from §6.4 |
| `subagent_steer` | `{subagent_id, text}` → `{ok, queued, steer_id, session_status, note}` |
| `subagent_terminate` | `{subagent_id, confirm}` → `{ok, terminate_requested, note}` — refuses without `confirm:true` |
| `subagent_control` | `{subagent_id, op, …}` → see below |

### 6.1 `subagent_control`

`op` is exactly one of `pause` · `resume` · `set_runtime`.
`set_runtime` also takes `model`, `provider`, `effort` (`low`\|`medium`\|`high`); `""` clears an
override, and an absent key leaves it unchanged.

Success shape:

```json
{"ok": true, "note": "<honest semantics>", "session_status": "…",
 "paused": false, "model_override": "", "provider_override": "",
 "effort_override": ""}
```

### 6.2 Session object fields (added to `subagent_list[].sessions` and `subagent_transcript.session`)

`paused` (bool) · `model_override` · `provider_override` · `effort_override`. All four are
**always present** with their defaults, including for registry meta files written before this
amendment.

### 6.3 Turn-boundary semantics

**2026-08-03 amendment:** The human spawn-decision control and its approval hold were removed
end to end. Every delegation now auto-approves at every depth.

Controls are checked in this order, once per completion:

1. **Terminate** — unchanged, and still wins over every other hold.
2. **Pause hold** — parks the sub-agent with status `paused`.
3. **Runtime override** — `effort` scales the per-completion cap off the turn's ORIGINAL base
   (low 0.5× · medium 1.0× · high 2.0×, clamped to `[512, 16384]`); `model`/`provider` rebuild the
   provider via `config.get_provider`. A failed rebuild logs `subagent_model_override_failed` and
   keeps the current provider — an override never breaks a turn.
4. **Steer drain** — unchanged.

There is no cap and no wall-clock timer. A pause parks until resume or terminate. The only
automatic release is controller liveness: if no controller has polled the registry within
`CONTROLLER_GRACE_S` (120 s), the pause releases because the only party that could ever resume
it is provably gone.

### 6.4 Lossless raw transcript (`chudware.subagent.raw/1`)

Each sub-agent has a true append-only JSONL log beside its owning conversation:
`<Sessions>/<conversation_id>.subagents/<subagent_id>.jsonl`. When no conversation transcript
directory is bound, the registry uses `<subagents_dir>/<subagent_id>.raw.jsonl` so the raw log
always exists.

Line 1 is written once:

```json
{"kind":"header","schema":"chudware.subagent.raw/1","subagent_id":"...","session_id":"...","role":"...","parent_id":"...","run_id":"...","created_ts":0.0}
```

Every subsequent line is one record with a monotonically increasing `seq`, starting at 1:

```json
{"ts":0.0,"seq":1,"kind":"message","message":{"role":"assistant","content":"..."}}
{"ts":0.0,"seq":2,"kind":"lifecycle","event":"started|paused|resumed|terminated|finished|steer","note":"..."}
{"ts":0.0,"seq":3,"kind":"compaction","before":0,"after":0,"messages":[{"role":"user","content":"..."}]}
```

The `message` value and every entry in a compaction's `messages` array are the verbatim,
uncapped provider message dictionaries. Nothing in this raw log is ever truncated, summarized,
or pruned. It is distinct from the display transcript, `<id>.transcript.jsonl`, which remains a
human-readable lifecycle-and-reply stream.


## 7. Manufacturing gateway (mfg_*)

This section amends §5.1 and allocates all manufacturing sections. The `hello.params.client`
vocabulary additionally accepts `observer`. An observer connection may use read/query/subscribe
methods, but MUST reach no write dispatch path, cannot claim a `commander` lease, and cannot issue a
run or approval mutation (`MFG-OBS-1`).

Every gateway response uses the structured error envelope from `contracts/doc_ops.md` §5. Successful
results carry `result_seq` and `epoch`; every referenceable result row carries its establishing
durable `seq`. Canonical shared types and full schemas are in
`contracts/manufacturing_gateway.md`.

| method | params → result |
|---|---|
| `mfg_device_list` | `{}` → `{devices:[{device_id,machine_class,state,capability_summary,lease}]}` |
| `mfg_device_describe` | `{device_id}` → `{identity,fingerprint_sha256,capability,transport,policy}` |
| `mfg_device_claim` | `{device_id,role:"commander"|"observer",ttl_ms?}` → `{lease}` or Tier-0 refusal |
| `mfg_device_release` | `{lease_id}` → `{released:true}` |
| `mfg_device_heartbeat` | `{lease_id}` → `{lease,ttl_ms}` |
| `mfg_device_snapshot` | `{device_id}` → `{snapshot:FreshValue<ControllerSnapshot>,raw}` |
| `mfg_device_subscribe` | `{device_id,kinds[]}` → `{subscribed:true,from_seq}` and establishes a subscription lane |
| `mfg_capability_report` | `{device_id}` → `CapabilityReport` |
| `mfg_policy_get` | `{device_id}` → `{machine_class,attendance_allowed[],spindle_authority,max_exercisable_by,emulator_stage_passed}` |
| `mfg_run_propose` | `{device_id,artifact:{path\|bytes_ref,kind},job,snapshot}` → `{run_id,state:"draft",tier0,gate_report_ref}` |
| `mfg_run_submit` | `{run_id}` → `{state:"awaiting_approval"\|"draft",gate_report_ref}` |
| `mfg_approval_issue` | `{run_id,approver_auth,exercisable_by,expiry_ttl_ms,attendance,spindle_confirmation?}` → `ApprovalBinding`; human seats only |
| `mfg_approval_revoke` | `{approval_id,reason}` → `{revoked:true,at_seq}` plus falsification push |
| `mfg_approval_describe` | `{approval_id}` → frozen `ApprovalBinding` |
| `mfg_run_start` | `{run_id,approval_id,initiator}` → `{state,nonce_burned_at_seq,gate_report_ref}` or named refusal |
| `mfg_run_hold` | `{run_id,kind:"feed_hold"\|"controlled_stop"\|"door_suspend",initiator}` → `{state,at_seq}` |
| `mfg_run_resume` | `{run_id,initiator}` → `{state}` or named no-resume refusal |
| `mfg_run_abort` | `{run_id,initiator}` → `{state:"aborting"}` |
| `mfg_run_describe` | `{run_id}` → `{state,approval_id,initiator,gate_report_ref,artifact_hash,timeline_from_seq}` |
| `mfg_run_list` | `{device_id?,state?,limit?}` → `{runs[]}` |
| `mfg_event_query` | `{from_seq,kinds[]?,subject?,limit?}` → `{records[],head_seq,waiver_rates}` |
| `mfg_event_subscribe` | `{from_seq,kinds[]?}` → `{subscribed:true,from_seq}` and establishes a subscription lane |
| `mfg_tier0_register` | `{domain_id,schema_version,rows[]}` → `{registered,rejected[],totality,at_seq}` |
| `mfg_event_append` | `{domain_id,kind,subject,result_kind,body,raw?,derived_from?}` → `{seq,epoch,ref}` |
| `mfg_result_invalidate` | `{domain_id,class,reason,why,affected[],superseded_by?}` → `{at_seq}` |

`mfg_run_propose` and `mfg_run_submit` never touch a device. No method in this section jogs, homes,
zeroes, unlocks, changes an override, or writes controller settings.

### Manufacturing section allocation (`MFG-SECT-1`)

The contract is append-only. Each domain owns exactly its allocated future section; no domain chooses
another number.

| section | domain | contents |
|---|---|---|
| `## 7. Manufacturing gateway (mfg_*)` | gateway | gateway methods above |
| `## 8. Manufacturing monitor (read-only)` | monitoring | `mfg_monitor_*` |
| `## 9. Manufacturing cutting` | cutting | `mfg_cut_*` |
| `## 10. Manufacturing turning` | turning | `mfg_turn_*` |
| `## 11. Manufacturing PnP` | PnP | `mfg_pnp_*` |
| `## 12. Manufacturing robot` | robot | `mfg_robot_*` |
| `## 13. Manufacturing twin` | twin | `mfg_twin_*` |
| `## 14. Manufacturing additive` | additive | `mfg_print_*` |
| `## 15. Manufacturing metrology` | metrology | `mfg_metrology_*` |
| `## 16. Manufacturing perception` | perception | `mfg_perception_*` |

## 17. Exact perspective document camera (append-only, 2026-08-09)

`ping.result.capabilities` includes `doc_render_view.camera_pose.v1` when the running worker
implements this section. The capability applies to the existing `doc_render_view` method; it does
not allocate a second renderer.

When `view.camera` is present it is authoritative. Its request form is:

```json
{
  "camera": {
    "projection": "perspective",
    "position": [40.0, 20.0, 30.0],
    "target": [0.0, 0.0, 5.0],
    "up": [0.0, 0.0, 1.0],
    "vertical_fov_deg": 45.0,
    "near": 0.1,
    "far": 1000.0
  },
  "width": 640,
  "height": 480
}
```

For compatibility with direct renderer callers, integer `width` and `height` MAY instead be
nested in `camera`; nested values win if both forms are supplied. Image dimensions are the only
camera-related values that clamp, to `[64,2048]`. A successful response reports the applied
dimensions in `camera.image_width_px` / `camera.image_height_px`, sets `image_size_clamped`, and,
when clamped, includes `requested_image_width_px` / `requested_image_height_px`. Position, target,
up, FOV, and clip planes never clamp or auto-fit.

The exact camera uses WCS coordinates. Let `forward = normalize(target-position)`,
`right = normalize(cross(forward,normalize(up)))`, and `true_up = cross(right,forward)`. The
rasterizer uses vertical FOV and aspect ratio exactly, clips triangles against the near, far, and
four side-frustum planes (`near <= depth <= far`), and performs reciprocal-depth testing. It does
not replace the target with a body centre, choose another up vector, frame a bounding box, or
substitute orthographic projection. A
`camera` request combined with legacy `focus` is refused rather than silently changing either
contract.

A successful exact-camera response retains the legacy render fields and adds:

```json
{
  "capability": "doc_render_view.camera_pose.v1",
  "camera": {
    "position": [40.0, 20.0, 30.0],
    "target": [0.0, 0.0, 5.0],
    "up": [0.0, 0.0, 1.0],
    "vertical_fov_deg": 45.0,
    "image_width_px": 640,
    "image_height_px": 480,
    "near": 0.1,
    "far": 1000.0
  },
  "revision": 17,
  "image_size_clamped": false
}
```

`camera` is the exact applied pose and projection, and `revision` is the document revision held
under the render lock while the mesh and pixels are produced.

### 17.1 Perspective `frame`

An exact-camera response returns this reprojectable frame:

```json
{
  "projection": "perspective",
  "position": [0.0, -10.0, 0.0],
  "right": [1.0, 0.0, 0.0],
  "up": [0.0, 0.0, 1.0],
  "forward": [0.0, 1.0, 0.0],
  "vertical_fov_deg": 45.0,
  "tan_half_vertical_fov": 0.41421356237309503,
  "focal_length_px": 579.4112549695428,
  "principal_x_px": 320.0,
  "principal_y_px": 240.0,
  "near": 0.1,
  "far": 1000.0,
  "width": 640,
  "height": 480
}
```

The example basis is the resolved orthonormal basis for a camera at `[0,-10,0]` aimed at the
origin with WCS +Z image-up. To project WCS point `p`, compute `q=p-position`,
`depth=dot(q,forward)`,
`x=dot(q,right)`, and `y=dot(q,up)`. If depth is outside `[near,far]` the point is clipped.
Otherwise:

```text
pixel_x = principal_x_px + focal_length_px * x / depth
pixel_y = principal_y_px - focal_length_px * y / depth
```

This differs from the unchanged orthographic frame. Orthographic frames have no `projection` key
and retain `{center,right,up,dir,scale,cu,cv,width,height,clip_depth,focused,clip_near}` with:

```text
q = p-center
pixel_x = (dot(q,right)-cu)*scale + width/2
pixel_y = height/2 - (dot(q,up)-cv)*scale
```

Orthographic apparent scale is depth-independent; perspective apparent scale is proportional to
`1/depth`.

### 17.2 Named exact-camera refusals

Exact-camera validation returns `ok:false`, an empty `path`, and one of these stable `code` values;
it never falls back to a named/ISO view:

| code | condition |
|---|---|
| `camera_invalid` | camera is not an object, or pose vectors are missing/non-finite |
| `camera_projection_unsupported` | an explicit projection is not `perspective` |
| `camera_zero_direction` | position and target coincide |
| `camera_up_degenerate` | up has zero length |
| `camera_up_parallel` | up is parallel to the view direction |
| `camera_fov_invalid` | vertical FOV is outside `(0,179)` or cannot yield a finite focal length |
| `camera_clip_range_invalid` | clip range does not satisfy `0 < near < far` |
| `camera_image_size_invalid` | a supplied image dimension is not an integer |
| `camera_focus_conflict` | authoritative camera and legacy auto-fit focus are both present |

## 18. Polyhedral-FEA mesher cutover (append-only, 2026-08-30)

Polyhedral-FEA is the sole in-process FEA volume mesher. `sim_mesh` accepts:

```text
{step_path, fineness?, order?|mesh_order?, target_size_mm?, min_size_mm?,
 max_size_mm?, learned_advisor?, advisor_max_dof?}
```

It returns the existing flat tet4/tet10 `points`/`tets` envelope and quality fields.
`sim_run_static`, `sim_run_modal`, `sim_run_thermal`, and `sim_run_fatigue` use the same
mesh controls when their study carries `step_path`.

The former Netgen/structured-mesh controls are removed rather than aliased:
`density`, `curvature`, `curv_elems_per_circle`, `optimize`, `mesher_backend`,
`structured`, `mesh_type`, `auto_type`, `quad_surface`, `fallback_to_tet`,
`thin_wall_layers`, and `algorithm3d`. A STEP-backed request containing one of these keys
returns `ok:false` with a message naming the removed key; it is never silently ignored or
mapped to the Polyhedral-FEA path.

## 19. Display tessellation quality (append-only, 2026-09-04)

| method | params → result |
|---|---|
| `doc_set_display_quality` | `{doc_id, quality: "draft"\|"standard"\|"fine"}` → ops-response (doc_ops §1) + `{ok:true, quality, triangles:int}` |

`triangles` is the display-triangle total across the document's bodies after re-tessellation, so a
caller can see what the tier cost. The response carries the usual `tess.aicm_path`, which is the
mesh the client reloads; the document's revision does NOT change (view-only, like
`doc_set_explode_state`).

An unknown `quality` name is refused with `{"code":"invalid_op"}` — it is never silently mapped to
a tier. Per-tier deflections, the `CHUDWARE_DISPLAY_QUALITY` default override, and the
cache-invalidation rule are specified in doc_ops.md §7.

## 20. Measured cylinder axis — `eval_fit_cylinder` (append-only, 2026-09-04)

`eval_fit_cylinder {doc_id, body?, near:[x,y,z], search_radius_mm?}` → `{axis_point:[3],
axis_dir:[3], radius_mm, rms_residual_mm, n_points_used}` · `body` is 1-based, `<=0`/absent
selects the LAST body (the Evaluate-family convention shared with `eval_mass`/`eval_check`)
· `near` is REQUIRED (a world point on or near the wanted cylindrical surface; absent →
`{"code":"invalid_op"}`) · `search_radius_mm` defaults to 5.0 and must be > 0.

Every display-tessellation vertex of the selected body within `search_radius_mm` of `near`
is fed to a least-squares cylinder fit (`core/geom/fit.cpp`, PCA seed + Gauss-Newton refine
of axis point/direction/radius). `axis_dir` is a unit vector whose SIGN is arbitrary;
`axis_point` is the axis point nearest the sampled centroid (an axis point is only defined
up to a slide along the axis). `rms_residual_mm` is the fit's own evidence — a merge of
independently sourced CAD should gate on it, not on the axis alone.

Fewer than 6 culled vertices, a non-positive `search_radius_mm`, or a non-convergent
(degenerate/collinear) cloud return `{"code":"invalid_op"}` naming the point count. A
plausible-looking fit is never synthesized from an under-determined cloud.

## 21. Per-step motion collisions — `simulate_motion` (append-only, 2026-09-04)

With `check_interference:true`, every entry of `simulate_motion`'s `trajectory` now carries
`collisions: [{a, b, overlap_volume_mm3}]` beside its existing `interference: bool` — the
offending pairs AT THAT SAMPLE, from the same `interference_report` the check already ran.
Solid overlaps report their real common volume; a pair flagged only for violating
`min_clearance_mm` reports `overlap_volume_mm3: 0.0`. The list is empty on a clean step and
absent when `check_interference` is false.

The report's `overlap_mesh`/`overlap_bbox` are deliberately NOT carried into the trajectory
(a several-hundred-step sweep would carry a mesh per pair per step); they remain available
from `eval_interference` at a chosen pose. The top-level `interference` block is unchanged —
`first_collision_step`, `first_collision_value` and the full first-collision `detail` still
describe the FIRST offending sample, so a caller that only wants the first hit is unaffected
while a swept-clearance proof can now cite every step.
## 22. Static summary reduction disclosure — `sim_run_static` (append-only, 2026-09-05)

`summary.max_von_mises_mpa` / `summary.min_fos` are reduced over the node-averaged von Mises
field with nodes inside a two-element ring around every fixed node EXCLUDED (the fixture
singularity mask that has always applied). That decision is now disclosed: `summary` gains
`max_von_mises_reduction` (the literal rule), `max_von_mises_raw_mpa` and `min_fos_raw` (the
unmasked extrema), `summary_nodes_total`, `summary_nodes_excluded`, `reduction_masked_keys[]`
and `reduction_unmasked_keys[]`. The reply carries a top-level `summary_node:[…]` per-node 1/0
mask (length `n_nodes`), and the `.vtu` written for `study.vtu_path` carries the same mask as a
`summary_node` point-data array beside the unmasked `von_mises` array, so a viewer and the
packet agree by construction. When masked and raw peaks differ by more than 25 % a plain-language
entry is appended to `warnings`. `run_fea_convergence` rows pass the same fields through.
`sim_cache` schema bumps `/3 -> /4`; a `/3` entry reported the masked peak without disclosure.

## 23. Load sign convention — `sim_run_static` study loads (append-only, 2026-09-05)

A `pressure` load of magnitude `p` (MPa) is deposited work-consistently along the INWARD face
normal: `+p` pushes into the face (internal pressure on a bore), `-p` pulls outward (external
pressurisation / suction). Force loads are vectors in the global frame and carry no implied
sign. Before this date the engine deposited `+p` along the OUTWARD normal, contradicting every
tool docstring; the fix is in the engine and is pinned by ctest `fatigue` ("pressure load acts
along the INWARD face normal"). `sim_run_adaptive` and `sim_run_fatigue` share the deposit.

## 24. Elastic-plastic material — `sim_run_static` `material.plasticity` (append-only, 2026-09-06)

`study.material` accepts an optional `plasticity` object that switches the static and the
contact solve from linear elasticity to SMALL-STRAIN J2 (von Mises) plasticity with linear
isotropic hardening. Absent, `null`, or `{"enabled": false}` is the linear-elastic solve
every existing caller gets, byte for byte.

```
material.plasticity = {
  "enabled":        true,      // optional, default true when the object is present
  "yield_mpa":      276.0,     // initial yield; omitted uses material.yield_mpa
  "hardening_mpa":  1200.0,    // post-yield TANGENT MODULUS in MPa. 0 = perfectly
                               //   plastic. NOT a ratio of E; refused at >= E_mpa
  "max_increments": 10,        // load steps over [first yield, full load], 1..1000
  "tol":            1e-3       // relative residual ||r|| / ||applied load||, in (0,1)
}
```

The law is `sigma_y(eq_p) = yield_mpa + hardening_mpa * eq_p`, integrated by radial return at
every Gauss point with the CONSISTENT (algorithmic) tangent, under an incremental
Newton-Raphson load-stepping loop whose every tangent solve goes down the same
CHOLMOD/LDLT/SparseLU ladder and through the same process-wide solve gate as a linear solve.
Supported for tet4, tet10 and hex8. When `study.contact.enabled` is also set, the ACTIVE-SET
CONTACT LOOP RUNS INSIDE EACH LOAD INCREMENT — contact state and plastic state both advance
per increment — which is the only ordering that can grow a bearing patch as the edge under it
yields. An unknown key inside `plasticity` is REFUSED, never ignored.

The ladder does not start at zero load. Phase 0 solves the whole load with an infinite yield
stress (the linear-elastic contact solve), which yields the elastic peak equivalent stress
`q_max`; the requested increments then run over `[0.95*yield/q_max, 1]`, the only part of the
path where material state evolves. `q_max <= yield` returns the elastic answer with an empty
plastic state and `increments: 1`. This is not an optimisation: a contact interface at a small
fraction of its load is a marginal regime whose active set has no fixed point (the elastic
solver itself reports `converged:false` there), and marching from zero walks into it.

Reply, on success and on refusal, as a TOP-LEVEL sibling of `summary`:

```
plasticity = {enabled, yield_mpa, hardening_mpa, tol, requested_increments,
              increments, newton_iterations_total, converged, load_fraction,
              max_equivalent_plastic_strain, plastic_volume_mm3, plastic_fraction,
              yielded_nodes}
```

`increments` counts the increments that CONVERGED (adaptive substeps included, so it may
exceed `requested_increments`, and it includes the one that reaches first yield).
`load_fraction` is the fraction of the applied load the last converged increment carried — 1.0
on a converged run, and the limit load of the model on a refusal.
`max_equivalent_plastic_strain` is the GAUSS-POINT peak, not the peak of the averaged nodal
field. `plastic_volume_mm3` is the integration volume whose Gauss point has yielded and
`plastic_fraction` its share of the model.

`summary.max_von_mises_mpa` remains the stress with its existing fixture-ring reduction
(section 22) — it is not replaced by anything plastic — but it is now bounded by the flow
stress `yield_mpa + hardening_mpa * max_equivalent_plastic_strain`, which is the observable
difference from an elastic run of the same study. The reply gains a top-level nodal
`equivalent_plastic_strain[]` (length `n_nodes`, the mean of each incident element's own
Gauss-point mean), and the `.vtu` written for `study.vtu_path` carries it as an
`equivalent_plastic_strain` point-data array beside `von_mises` and `summary_node`.

Two refusals, both physics rather than preference:

* **Plastic collapse.** A load past the model's limit load has NO equilibrium. When an
  increment will not converge even after the step has been halved to below 1/32 of the
  requested increment, the run is `ok:false` with an error naming the load fraction reached,
  and the `plasticity` block rides on the refusal. The last Newton iterate is never reported:
  it is not a stress state.
* **Beyond small strain.** A peak equivalent plastic strain above 0.05 appends a plain
  `warnings` entry (`plastic_strain_beyond_small_strain: …`). The mesh is never updated and no
  area reduction is modelled, so past a few percent the patch the solve spread the load over is
  not the patch the part has.

`material.plasticity` is REFUSED, not ignored, by `sim_run_modal` (eigenvalues linearize about
an unstressed state and stay elastic), `sim_run_thermal` (no stress for a yield surface to
bound), linear buckling (a critical multiplier is defined only for a linear pre-buckling
response) and any study carrying `adapt` (the ZZ error estimator is derived for linear
elasticity). At the MCP seam it is the `plasticity=` parameter of `run_fea_static` and
`run_fea_contact` and is NOT accepted inside the `material` dict, so it cannot reach a tool
that would silently drop it.

## 25. Size-band floor at the MCP seam — `study.min_size_mm` (append-only, 2026-09-07)

`study.min_size_mm` is unchanged in the engine (section 18 already lists it among the
`sim_mesh` controls, and `sim_run_static` reads it from the study through
`core/sim/fea_run.cpp resolve_mesh`). What is new is that three MCP tools can state it:

| tool | parameter | reaches |
|---|---|---|
| `run_fea_static` | `min_size_mm?: float` | `study.min_size_mm` |
| `run_fea_convergence` | `min_size_mm?: float` | every leg's `study.min_size_mm` |
| `run_fea_contact` | `min_size_mm?: float` | `study.min_size_mm` |

It is the element-size band FLOOR in mm — `MeshSizeBand.min_len`, which defaults to half
the base size — and it is what bounds the mesher's feature-resolution descent
(`core/sim/mesher.cpp`). Without it a study whose feature needs less than half its own
element size is REFUSED with "polymesh needs an element size of X mm to resolve this
feature, below this study's element-size band floor of Y mm — raise fineness or set
min_size_mm to mesh it"; that refusal now names a parameter the caller has.

Validation at the seam: finite and `> 0`, and never greater than the size it floors —
`mesh_size_mm` for `run_fea_static` / `run_fea_contact`, the FINEST `sizes_mm` entry for
`run_fea_convergence`. A floor above the stated size would raise the base size instead
(`core/sim/mesh_size_band.cpp` clamps the base up to the floor), silently coarsening the
mesh the caller just named, so it is a `ValueError` rather than a solve.

It is echoed exactly where the stated size is echoed today: the persisted study record
beside `target_size_mm`, and each `run_fea_convergence` table row beside `size_mm`. Both
appear only when stated.

## 26. Delivered discretization — `mesh_quality.char_len_mm` (append-only, 2026-09-07)

Every result that carries `mesh_quality` (`sim_run_static`, its contact and refusal
envelopes, `sim_run_modal`, `sim_run_thermal`) now carries `mesh_quality.char_len_mm`
beside `n_elems`: the DELIVERED characteristic element length in mm of the mesh that was
solved — `MeshOut::char_len_mm` (`core/sim/mesher.h`), the smallest part's value on a
multi-body mesh — not the requested `target_size_mm`. It is `null`, never `0`, on a route
with no mesh length to report (an inline `points`/`tets` study Chudware never sized, which
includes every `adapt` pass; that route states its size per pass in
`adapt.passes[].element_size_mm`).

This exists because the two numbers routinely differ: the memory governor sizes against an
element budget (`core/sim/mesh_size_band.cpp`), and a convergence ladder requested at
1.6 / 1.2 / 0.9 mm was delivered at 5.62 / 5.43 / 5.42 mm with the substitution visible
only in governor prose.

At the MCP seam, `run_fea_convergence` puts it on every solved table row as
`delivered_size_mm` (`size_mm` stays the REQUESTED size) and adds a reply-level
`size_delivery: [{leg, requested_mm, delivered_mm, ratio}]`, one entry per solved leg,
`ratio = delivered/requested` (`null` when either side is unavailable, e.g. a `fineness`
rung). When any leg's ratio leaves `0.8x .. 1.25x`, the reply's existing `note` gains one
sentence naming those legs — no second note field. The per-leg `.vtu` filename keeps
carrying the REQUESTED size (`_h<size>_`); `delivered_size_mm` is the truth about the mesh
behind the file.

## Assembly placement provenance persistence (append-only, 2026-09-07)

Assembly placement provenance uses the existing `doc_set_meta` / `doc_get_meta`
document-metadata RPCs under key `placement_sources`. Its value maps component IDs
to `{"placement_source": "transform" | "mate" | "origin", "at_revision": integer | null}`.
`at_revision` is the engine revision at placement; metadata writes mark the document
dirty without advancing its geometry revision.

This record is part of the document JSON inside the `.step+` container, not process
memory or a separate file. Save, close, and reopen retain the provenance even though
the reopened document has a fresh runtime ID. A persisted `"mate"` record takes
precedence over later starting-pose `"transform"` notes. Missing records (including
imported components without recorded provenance) remain `null`, never inferred.

`list_world_bodies` and `describe_assembly` expose `bodies[].placement_source` from
this persisted record; `describe_assembly` retains its placement narrative alongside
the structured `bodies` result.

## Steady thermal part-mesh evidence (append-only, 2026-09-07)

`run_fea_thermal` without `transient` now dispatches `sim_run_thermal` without a
`study.transient` object, using the same part STEP meshing and face-selector
resolution as transient conduction. The engine runs `fea_thermal_solve`; fixed
temperature fixtures, constant heat-flux loads and convection are supported.
Time schedules require the transient route. No CalculiX executable is required.

Successful steady packets carry `summary`, `temperature_max_c`,
`temperature_min_c`, `flux_max_w_m2` (maximum Fourier flux magnitude sampled at
element integration points), `n_nodes`, `n_elems`, `mesh_quality`, and
`provenance.solver="engine"`. An optional absolute `vtu_path` writes the actual
part mesh with nodal `temperature`. Nodal temperatures persist in the document
study. Steady packets have no frame/time axis.

## 27. Engine linear buckling — `sim_run_buckling` (append-only, 2026-09-07)

This supersedes the historical CalculiX box-proxy `sim_run_buckling` row in section 4.
The only route is the native engine: `chudware_call("sim_run_buckling", params_json,
&out_json)` follows the modal C ABI (release the reply with `chudware_free`), and
the worker and Python core bridge preserve that route without a CalculiX fallback.

Request: `{study, n_modes?: 4}`, with integer `n_modes` from 1 through 100.
`study` has the static study's real STEP path or flat inline `points`/`tets`
plus `nodes_per_elem` (4 or 10), elastic
`material:{E_mpa,nu}`, fixtures, and reference force/pressure loads. It uses the
same exterior-face resolution and tet10 work-equivalent force/pressure
distribution as static; positive pressure is MPa inward. A STEP study defaults
to quadratic tet10 and accepts the current shared mesh sizing controls. Inline
connectivity is never reinterpreted by mesh order. `job_id` remains cancellable
through `sim_cancel`.

The engine solves the elastic reference-load state, assembles its geometric
stiffness, then solves the linear buckling eigenproblem. The requested modes are
the smallest positive finite load factors. No compressive reference stress or no
positive buckling modes is an `ok:false` refusal with `error` and `reason`, not an
Euler estimate, skipped success or a fabricated positive factor. Plasticity,
contact/bonded-gap interfaces, and adaptive-static requests are refused rather
than ignored.

Successful packets carry these top-level fields:

* `modes:[{load_factor, participation:{x,y,z}}]`, `load_factors`, `n_modes`,
  `n_nodes`, `n_dof`, `n_elems`, and flat `mode_shapes[n_modes*n_nodes*3]`.
  Each shape includes fixed-node zeros and is normalized to maximum absolute
  component 1; it has arbitrary amplitude, not a physical displacement in mm.
* `participation` is a directional shape measure:
  `p_c = sum_i(phi[i,c]^2) / sum_i,c(phi[i,c]^2)`. Its nonnegative x/y/z shares
  sum to 1 and are independent of mode sign/amplitude. This is not effective
  modal mass, which is not defined by this buckling eigenproblem.
  `participation_measure` names that reduction.
* `critical_load_factor` is the first positive factor.
  `reference_resultant_n` is the vector sum of the assembled reference nodal
  forces; `reference_load_n` is its magnitude. `critical_load_n` equals
  `critical_load_factor * reference_load_n`, not the sum of load magnitudes.
  A zero resultant (including numerical cancellation relative to the applied
  forces) is refused: a scalar critical resultant load is undefined for
  self-equilibrated loading, even if that loading produces compression.
* `mesh_quality`, `mesh_type_used`, `solver_notes`, `solver:"engine"` and
  `provenance` with `solver:"engine"`, `analysis:"linear_buckling"`,
  `engine_build_id`, `polymesh_commit` and timestamp.

An optional absolute `study.vtu_path` writes the actual solved mesh with one
three-component point field `mode_1`, `mode_2`, ... for each normalized shape,
using the modal VTU convention. Its path is echoed only after the write succeeds.
Failures retain mesh evidence when it was resolved; pre-solve refusals have null
critical values rather than a plausible zero.

### Buckling search ceiling (2026-09-08)

`sim_run_buckling` also accepts top-level `max_load_factor`, finite and positive,
default `10000`. `run_fea_buckling` exposes the same parameter. This is an explicit
engineering search ceiling: a critical load at or above that multiple of the
reference load is reported as no buckling mode below the chosen ceiling, not as
infinite strength. Results and in-band refusals echo `max_load_factor`.

The reciprocal pencil is `K_G phi = mu K phi`, with `lambda = -1/mu`. The negative
pivot count of `LDLT(K_G + epsilon K)`, `epsilon = 1/max_load_factor`, determines
how many modes satisfy `0 < lambda < max_load_factor`. Zero count returns
`ok:false` and a reason naming the ceiling and epsilon; otherwise the shared
CHOLMOD/LDLT Cholesky eigen path requests at most that count and `n_modes`.
No element stresses are filtered and no load direction is special-cased.

## 28. Per-body static/contact materials (2026-09-08)

`sim_run_static` accepts the existing single `study.material` object or
`{"per_body":{"1":{"E_mpa":200000,"nu":0.3,"yield_mpa":500},
"2":{"E_mpa":110000,"nu":0.34,"yield_mpa":125}},"default":{...}}`.
Keys are canonical positive, **1-based STEP solid ordinals**. `default` is optional
when every meshed ordinal has an assignment. Each material requires finite positive
`E_mpa` and `-1 < nu < 0.5`; `yield_mpa` defaults to 250 MPa. Optional `density`
is finite and positive. Density is echoed, not used by these static solves.
An unknown ordinal or missing assignment is refused naming the ordinal. A fused
mesh group with different effective materials is refused naming its source
ordinals; declare contact or bonded interfaces to preserve separate bodies.

Stiffness assembly, stress recovery and plastic radial return use a compact
per-element material index, attributed through the body's first-node range.
With heterogeneous plasticity, each body's yield is authoritative; the study's
plasticity hardening/increment/tolerance settings are shared. Contact penalties
and explicit bonded-gap springs use the **softer Young's modulus of the two
bodies** at each pair. `material.interface_stiffness_rule` and
`contact.penalty_material_rule` report `softer_body_E`. Heterogeneous contact
reports the actual `penalty_stiffness_range_n_per_mm`; its legacy scalar
`penalty_stiffness_n_per_mm` is null rather than claiming one stiffness.

Packets report `provenance.material_mode:"single"|"per_body"`. In per-body mode,
`material.per_body` echoes every actually used ordinal's effective properties
and zero-based `material_id`; `material.body_summary` gives each body's
`max_von_mises_mpa` and `min_fos` over summary-selected elements.
`element_von_mises` is the peak recovered stress within the element before nodal
averaging. `element_factor_of_safety` uses that element's own yield, capped at 15;
heterogeneous `summary.min_fos` is its minimum over elements incident to at least
one summary-selected node. The raw minimum includes every element. The nodal
`factor_of_safety` map uses the minimum incident element FoS. Existing single
material summary semantics remain unchanged. The VTU contains a UInt16
`material_id` cell array and the nodal `factor_of_safety` field.

MCP `run_fea_static(material=...)` and `run_fea_contact(material=...)` accept this
same shape. Existing material-name strings still resolve through the material
lookup; names are also accepted in `per_body` entries and `default`. MCP resolves
them to physical property objects before submitting the study.

## 29. Motion axis-line evidence (2026-09-08)

`simulate_motion` preserves the complete driven mate `value` object while changing
its scalar value. An angle drive resolves a hinge LINE from `value.axis` plus
`value.axis_point`, the angle mate's own third axial reference, or a concentric/hinge
mate on the driven body. The line's authored coordinates are local to the first
reference component; resolved geometry is carried into world coordinates. The
angle driver needs one grounded and one floating reference. An unidentifiable line
is refused with `ok:false`, `reason:"driver_axis_line_unresolved"` and a failed
`pose_check`, never a successful origin-pivot sweep.

For each sample, the rigid driver is `D=[R, p-Rp; 0,1]`, composed as `D*T_initial`;
the angular increment is relative to the initial mate value. Every sample is still
solved against the complete mate network, and a solved pose that departs from the
driver is refused (`driver_pose_mismatch` in frame issues). Native trajectory
matrices remain column-major but retain double precision; the MCP seam exposes the
established row-major representation.

`simulate_motion` and `sweep_envelope` replies add
`pose_check:{ok,frames:[{i,ok,bodies:[...]}]}`. Each rotationally driven body's check
contains `component`, world `axis_point`, unit `axis_dir`, `translation_ok`,
`translation_error_mm`, `rotation_ok`, `invariant_coordinate`, and `ok`. Translation
is checked on `T_sample*inverse(T_initial)`, not the pre-existing body placement,
against `p-Rp` with tolerance `1e-6*max(1,|p|)` mm. Rotation must match the requested
Rodrigues rotation to `1e-6`. For a world-aligned axis, `invariant_coordinate` is
`{axis:"x"|"y"|"z",ok}` and checks the whole affine coordinate row, hence every
point and both extrema; otherwise it is null. Distance sweeps have no rotational
body checks. Loop playback repeats the first frame's check along with its pose.

Steering checks include floating components carried by each driven hub's
single-anchor mate island, not only directly driven hubs. Failed checks make the
tool `ok:false`; invalid frames do not produce interference evidence. Missing-line
refusals have no sampled frames. `asm_mate(value=...)` now accepts the same native
value object, allowing an angle-only hinge line to be authored through MCP.

Clarification to §29's grounding statement: grounding is preferred, not required
when the mate defines its line. The second measured body is the driver unless
it is grounded, in which case the first is driven with opposite angular sign.
Both-floating mechanisms retain their full mate solve; an incompatible solved
pose still fails `pose_check` rather than being accepted or assigned a new pivot.

## 30. Mixed bonded/contact interfaces and penalty control (2026-09-08)

`sim_run_static` accepts `study.interfaces`:
`{"bonded":[[1,2]],"contact":[[2,3]],"default":"contact"|"bonded"|"free"}`.
Body references at the engine seam are positive, 1-based STEP solid ordinals,
matching `material.per_body`. MCP `run_fea_static` and `run_fea_contact` also accept
exact document body names: `asm_world_bodies` records `source_ordinals` in STEP
export order, and a name resolves only when it identifies one solid. Unknown,
duplicate, or multi-solid names refuse with an ordinal-selection remedy.

Explicit unordered body pairs override the default. The omitted default is
`contact` when `study.contact.enabled` is true, otherwise `free`. Listing a pair
in both arrays refuses naming the pair; a nonexistent mesh ordinal refuses naming
the ordinal. Every interfaces declaration preserves separate bodies during meshing
rather than fusing touching solids. Candidate faces are paired only within
`contact.max_gap_mm` (default 0.5 mm). Bonded pairs use bilateral three-axis
`BondSpring` ties at their measured gap; an optional nonnegative `bond_gap_mm`
further limits that measured bonded gap. Contact pairs use the compression-only
active set; free pairs contribute neither ties nor contact. A global
`bond_gap_mm` plus contact without `interfaces` remains contradictory and refuses.

The packet's `contact.interfaces` contains `bonded` and `contact` arrays of
`{a,b,pairs,area_mm2}` using canonical ascending STEP ordinals, geometric candidate
counts, and tributary interface areas. `unmatched:[{a,b,type}]` reports declared
pairs for which no candidate survived the geometric/gap criteria, including on
the resulting disconnected-body refusal. Bonded candidates do not contribute to
the unilateral `contact.resolution` gap, area, seating or pressure guards.

MCP contact controls follow its flat-argument convention:
`penalty_scale=1.0`, `max_iterations=25`. Both are echoed in `contact`, including
resolution refusals. Penalty scale is finite and positive; iteration count is an
integer in 1..500. A chatter refusal names the penalty scale and iteration limit
it actually ran with. Reducing penalty softens only the unilateral normal penalty,
not the bilateral bonded ties. Press fits are bonded; running clearances are
contact, and both can now coexist in one material-mapped study.

The active-set release step preserves load-facing travel supports when dropping all
tensile trial pairs would disconnect a loaded component before it can move into its
seat (notably a concentric pin surrounded by positive clearance). Resultants are
summed over element-plus-bond components, so the pressed sleeve belongs to its
plate's support. Penetrating pairs are re-seated before this topology decision.
A retained travel guess is never accepted as converged contact: the final active
set must pass the ordinary compression-only gap test. A true lifting load with no
load-facing support still separates and refuses. Plastic contact uses the same
load-facing rule when recovering its travel seed.

For a durable job that ends `FAILED` with an engine `ok:false` packet,
`sim_results` retains its `contact`, `mesh_quality`, `reason`, `notes`,
`selection_spans_contact`, and `unfixed_islands` diagnostics when present, and
reports the engine error as `solver_error`. This includes delayed
`contact.interfaces.unmatched` and resolution/load-path refusals. The reply
remains `ok:false`; unconverged summary, displacement, and stress fields are
never promoted into results.

## 31. Independent native imports and complete assembly exports (2026-09-08)

MCP `import_part(path, ..., resume=False)` creates a new document by default.
For `.step+`, its fresh session has a unique document id/name and is detached from
the source save destination. Relative component paths retain their source-container
base. `resume=True` explicitly reuses an open document whose resolved path matches;
the reply carries `reused:true`. `into=<doc_id>` always adds a real import feature to
that target and never resumes the source. Subsequent transforms use that target's
returned feature ids; source documents remain unchanged.

Assembly exports recursively resolve path-backed native child assemblies in their
own local frames, then apply the parent placement. Every active component must
contribute at least one solid or export refuses with its name and source path.
Replies include `components:[{name,solids,source}]` in component-tree order, with
`source` retaining the component source object. Suppressed components are excluded.
Unsupported embedded-only sources also refuse explicitly rather than disappearing.

Interchange B-rep `validated:true` requires every re-read body to be valid and its
solid count to equal the world-geometry solid count exactly (a compound contributes
all its solids, not merely one body). Mesh writes and native document saves return
`validated:false`: nonzero bytes or an editable tree alone are not a solid-preserving
round-trip proof. A nonempty native export must carry `geometry.step`; a saver that
preserved only an unfinished tree cannot be advertised as a geometry export.

`get_part` ownership is scoped to its explicit `doc_id`. Its full form preserves the
complete engine `owner_map`; it does not cap or paginate feature ownership.

## 32. Motion reference bounds and placement-independent envelopes (2026-09-08)

Motion trajectories carry `reference_bodies:[{component,bbox:{min,max}}]` and
`reference_transforms:{component:[16 row-major values]}`. Bounds enclose the
actual WORLD-placed shapes at the reference pose, not their source-part AABBs.
Native dimensional motion uses its first solved sample; explicit MCP steering
uses the detached clone's solved pose before applying the drive. Native viewport
trajectory matrices remain column-major, while these reference matrices and MCP
trajectory matrices are row-major. Querying this reference does not re-solve or
mutate the caller's live document.

`sweep_envelope` moves each reference world's eight AABB corners by
`pose * inverse(reference_transform)`, then unions the resulting boxes. Thus a
pre-existing component placement or free wheel axle spin is not applied to a
source AABB and cannot manufacture ground penetration before steering starts.
The result remains a conservative bounding-box envelope, not an exact swept
solid; `fuse=True` still transforms and fuses source geometry using full placements.
`steering_turning_radius` uses these same bounds for wheel recognition, axle
centres and outer swept radius; the kinematic radius remains the intersection
of the posed wheel axle lines. Explicit steer axes remain WORLD lines and drive
on the left of the placement, with carried mate islands following the same
world rigid motion. Native `pose_check` reconstructs the expected rotation from
the requested world line and scalar value, independently of the drive matrix.

## Closed-fit contact idealization and bounded refinement (2026-09-08)

`run_fea_contact` forwards `closed_fit` (boolean, default true) and optional
`closed_fit_gap_mm` (finite nonnegative mm) to `study.contact`. The default
threshold is one quarter of the **measured** interface element size. Only
nonnegative gaps actually measured on analytic CAD faces and strictly below the
threshold are idealized as initially closed; facet-only/fallback gaps are not.
Raw CAD/facet gap measurements remain in the packet. Idealization changes only
the initial normal spring gap to zero: compression-only normal contact, free
tangential motion, active-set opening under tension, and load-path checks remain.
`closed_fit:false` preserves the clearance-resolved model and existing refusals.

`contact.resolution.mode` is `"closed_fit"` when any pairs were idealized, otherwise
`"resolved"` (the separate `resolved` boolean still determines acceptance).
The former carries `closed_fit:{pairs,gap_mm_max,limit_mm,note}`. The packet notes
state that the clearance was **not resolved**, and describe the bearing pressure
distribution as the conformal-fit upper bound. This bounds the conformal bearing
distribution, not the peak of a clearance-resolved Hertz contact: a conformal-fit
peak is checked between `P/(d*L)` and the finite-clearance Hertz line-contact peak,
not against a false claim of matching Hertz within ten percent.
Seatable-pair and geometric patch criteria use the initially closed pairs; the
post-solve minimum active population, equilibrium, and load-path criteria are
not relaxed. Sparse or unloaded patches still refuse with the required size.

Contact interface refinement requests a maximum direct influence depth of
`3*interface_size_mm`: two fine layers and one graded transition layer. The fill
stamp, graded field and bisection field honor that depth; conforming mesh closure
can add neighboring elements outside the directly marked band. Mesh notes report
the band depth, bisection waves, added elements and measured delivered size.
This bounds the direct refinement region, not total element count or process RAM.
The existing delivery refusal still applies if the bounded band cannot deliver
the requested interface size.

On Linux, successful solve provenance includes `peak_rss_gb` in GiB measured with
`getrusage(RUSAGE_SELF)` and `peak_rss_scope:"solver_process_lifetime"`. This is a
process-lifetime high-water mark, not an incremental estimate or per-study delta.

## Measured mesh-count governor (2026-09-08)

`sim_mesh` and STEP-backed static/contact results carry `provenance.governor`.
Its named `mode:"measured_octave"` preserves the existing hard mesh ceiling and
solve-memory admission. The unmeasured bounding-box envelope chooses only an
initial mesh-only probe (at most twice an explicit requested size); it is not
the requested study's measured cost. Subsequent mesh-count forecasts use a
geometry-local power fit and never span more than one octave in element size
without another mesh measurement. Actual measured counts, not forecasts,
decide whether the finer mesh fits the solve budget.

The record contains `estimate_gb` (GiB solve-peak envelope evaluated at
`estimated_elems`, the delivered mesh's actual count), `measured_probe` with
`h` (mm), `elems`, and `rss_gb`, and `probes` containing the measured sequence.
Probe `predicted_elems` is null for the unmeasured bootstrap or a combined-body
measurement; otherwise it is the preceding probe's bounded forecast and
`exponent` identifies that forecast's power. `model.count`, `model.solve_peak`,
`model.rss_stage`, and `model.dof_per_node` identify the model and measurement
stage. Probe RSS is process resident memory before solver assembly, not CHOLMOD
factor memory, a solve-peak measurement, or an incremental per-element cost.
Inline meshes that never ran the mesher have no mesh-probe evidence.
Static solve-memory refusals retain the governor record as well.

The contact refinement seed search uses the same explicit interface policy as
the solve. `interface_size_mm` refines contact-classified interfaces only;
bonded and free pairs do not seed that field. This prevents an explicitly
bonded sleeve exterior from launching a second fine shell through its entire
wall when only its interior running fit needs contact resolution.
Closed-fit successful summaries also carry `summary.contact_resolution_note`
so the unresolved-clearance disclosure remains visible beside stress and FoS.

The mesh-count probe scheduler samples at half-octave intervals. Its first
forecast uses exponent 1 (a 2x envelope for slopes from -1 through 3 over that
interval); subsequent forecasts fit measured local slopes, including negative
slopes on feature-driven fills. The maximum extrapolation remains one octave.
Simulation cache namespace `chudware-simcache/5` invalidates pre-governor and
pre-closed-fit results; old coarse meshes cannot masquerade as current evidence.

## Contact support-normal refusal (2026-09-08)

A frictionless contact-held solid whose applied resultant lies outside a
provably separating coordinate half-space of its admissible normal cone is
refused before matrix assembly or factorization. The proof uses the entire
candidate population, a superset of any active contact patch, not invented
active-pair forces. CAD closest-point directions are reused when available;
otherwise the mesh-facet direction is used. Floating bonded clusters are left
to the existing full-system checks rather than incorrectly testing each
constituent solid as an independently supported body.

The refusal carries `contact.load_path_refusal.criterion:
"normal_cone_excludes_load"`, one-based source-body `bodies` when a single
interface is involved, the separating `axis`, oriented `normal_range.min/max`,
normalized `load_direction`, and `supported_arc_deg` measured in the
load/separating-axis plane. `normal_population:"candidate_superset_of_active"`
and `normal_source` identify the evidence. Refining a clearance cannot supply
the missing reaction; no stress, displacement, or pressure summary is returned.
Closed-fit mode does not bypass this check and does not introduce friction,
opposing restraints, tensile ties, or a fictitious equilibrium.

Geometric and rigid-support preflight precede solve-memory admission; admission
still occurs before solver assembly. The refusal retains mesh counts,
`mesh_quality`, and `provenance` including measured governor and process-peak
RSS evidence. The same diagnostic fields survive synchronous contact
collection and durable `sim_results` failure collection.
