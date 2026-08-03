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

## 2. Doc worker (`engine/worker.py`) — method table

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

Implemented by `engine/worker.py` (`config_*`) over `engine/doc/session.py`
(`config_list`/`config_add`/`config_delete`/`config_activate`/`config_set_override`/
`config_set_suppression`); machine-checked by `engine/doc/test_configurations.py`.

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

Implemented by `engine/worker.py` (`weld_cut_list`) → `engine/doc/cutlist.py`
(`cut_list_for_session` forces a fresh uncached compile so the `weld_member` compiler repopulates
the `_weld_members` side-channel surfaced on `CompileResult.weld_members`, then `build_cut_list`
groups it); machine-checked by `engine/doc/test_weld_cutlist.py`. Host passthrough:
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
views. These methods live in `engine/doc/drawing.py` (registered via `drawing.WORKER_METHODS`) and
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
geometry) never crash the worker. Implemented by `engine/worker.py` → `engine/doc/export2d.py`
(+ `engine/doc/dxf.py`, `engine/doc/pdfvec.py`).

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

### Recognition & overlap (AI-experience wave — appended; see docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md)

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

## 3. Sim worker (`engine/sim/worker.py`, separate process, spawned on demand)

Same wire protocol. One job in flight; `job_id` correlates events.

| method | params → result |
|---|---|
| `ping` | → `{pong: true, protocol: 2, sim: true}` |
| `sim_mesh` | `{step_path, density: 0..1}` → `{mesh_path, nodes, elems, char_len_mm}` |
| `sim_run_static` | `{study}` (study JSON: material key + E/ν/ρ/yield, fixtures:[Selector→node sets prepared by doc worker as face point clouds], loads:[{faces, type: force\|pressure, vector\|magnitude}], mesh_path) → `{results_npz, overlay_aicm, summary:{max_von_mises_mpa, max_disp_mm, min_fos}}` |
| `sim_run_modal` | `{study, n_modes: 6}` → `{freqs_hz:[…], results_npz, overlay_aicm}` |
| `sim_run_flow` | `{stl_path, direction:[3], speed_mps, preset: "coarse"\|"medium"\|"fine"\|"xfine"}` → `{drag_n, lift_n, cd, frontal_area_mm2, reynolds_note, slices_aicm, lines_aicm, trace:[{step, drag}]}` |
| `sim_cancel` | `{job_id}` → `{cancelled: bool}` |
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
| `subagent_list` | `{}` → `{ok, sessions[]}` |
| `subagent_transcript` | `{subagent_id}` → `{ok, session, messages[], pending_steers[]}` |
| `subagent_steer` | `{subagent_id, text}` → `{ok, queued, steer_id, session_status, note}` |
| `subagent_terminate` | `{subagent_id, confirm}` → `{ok, terminate_requested, note}` — refuses without `confirm:true` |
| `subagent_control` | `{subagent_id, op, …}` → see below |

### 6.1 `subagent_control`

`op` is one of `pause` · `resume` · `approve` · `deny` · `set_runtime`.
`set_runtime` also takes `model`, `provider`, `effort` (`low`\|`medium`\|`high`); `""` clears an
override, an absent key leaves it unchanged. `deny` requires `confirm:true` and otherwise returns
`{ok:false, reason, requires_confirm:true}` — the same M16 gate `subagent_terminate` uses.

Success shape:

```json
{"ok": true, "note": "<honest semantics>", "session_status": "…",
 "paused": false, "awaiting_approval": false, "spawn_decision": "",
 "model_override": "", "provider_override": "", "effort_override": ""}
```

### 6.2 Session object fields (added to `subagent_list[].sessions` and `subagent_transcript.session`)

`paused` (bool) · `awaiting_approval` (bool) · `spawn_decision` (`""`\|`"approved"`\|`"denied"`) ·
`model_override` · `provider_override` · `effort_override`. All six are **always present** with
their defaults, including for registry meta files written before this amendment.

### 6.3 Turn-boundary semantics

Checked in this order, once per completion:

1. **Terminate** — unchanged, and still wins over every other hold.
2. **Approval hold** — a session with `awaiting_approval` and no decision parks (0.5 s poll).
   `delegation.request_delegation` now sets `awaiting_approval` whenever the routing approval
   policy is not `auto`, which is what releases the previously stranded depth>1 delegations
   (`routing.yaml` `approval_policy.deeper: overseer`, and nothing ever called
   `DelegationTree.approve()`). A denial returns `TurnResult(stop_reason="denied_by_user")`.
3. **Pause hold** — same park loop, status `paused`.
4. **Steer drain** — unchanged.
5. **Runtime override** — `effort` scales the per-completion cap off the turn's ORIGINAL base
   (low 0.5× · medium 1.0× · high 2.0×, clamped to `[512, 16384]`); `model`/`provider` rebuild the
   provider via `config.get_provider`. A failed rebuild logs `subagent_model_override_failed` and
   keeps the current provider — an override never breaks a turn.

Both holds are capped by `CHUDWARE_SUBAGENT_HOLD_MAX_S` (default 900 s). On expiry a **pause
auto-resumes**; an **approval hold denies, fail-safe** — an unapproved spawn never runs.
