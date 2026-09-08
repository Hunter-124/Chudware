# Contract: Document Ops — v1.1

> The closed vocabulary of document mutations. The GUI submits these; **the M9 AI modeler emits
> exactly these as its tool calls.** Design once, freeze here. Changes require a version bump.
>
> **v1.1 (C35-overhaul):** additive — new `set_rollback` op (rollback bar); `add_feature` mid-bar
> insert semantics; `sketch_set_dim` extended to update any subset of `value`/`expr`/`placement`/
> `driven`; dimensional `distance` gains an optional `axis` and a point-line ref form; dimensional
> constraints gain an optional `placement` annotation anchor. No existing field changed shape.

## 1. Envelope

Request (`doc_apply_ops`):

```json
{"doc_id": "…", "base_revision": 17, "ops": [ Op, … ]}
```

Response (also returned by `doc_undo`/`doc_redo`/`doc_rebuild`):

```json
{
  "revision": 18,
  "id_map": {"$0": "f9", "$1": "e12"},
  "tree": { …full document JSON (aicad.part/1 | aicad.asm/1)… },
  "rebuild": {
    "feature_status": [
      {"id": "f1", "ok": true,  "ms": 4.2, "error": null, "dangling_refs": []},
      {"id": "f5", "ok": false, "ms": 1.0, "error": "fillet failed: radius 12 exceeds…",
       "dangling_refs": ["edges[1]"]}
    ],
    "doc_status": {"active_sketch": null, "mates": null, "active_configuration": "Default"}
  },
  "tess": {"aicm_path": "…\\doc_6f0c_r18.aicm",
           "bodies": [{"body_id": 1, "name": "Body1", "color": [0.71,0.76,0.82]}]}
}
```

- `doc_status.active_sketch`: `"under" | "full" | "over" | null`; `doc_status.mates`:
  `"ok" | "over" | null` (assemblies). `doc_status.active_configuration`: the name of the
  configuration the returned geometry reflects (`"Default"` for documents without configurations —
  doc_schema.md §9 / worker_rpc.md §2.2).
- `id_map` maps batch placeholders (§3) to assigned ids.

## 2. Transactionality (normative)

1. **Validation is atomic**: every op is schema- and referentially-validated first; any failure
   rejects the whole batch with `invalid_op {index, reason}` and NO state change.
2. **Application is atomic**: all ops apply, revision increments by 1 per batch.
3. **Rebuild failures do not reject ops** (SolidWorks behavior): a feature that fails to compile
   keeps its edited definition and reports `ok:false` in `feature_status`; downstream features
   build against the last good body where possible.
4. Undo/redo operate on whole batches.

## 3. Id placeholders

Ops that create objects may be referenced by later ops **in the same batch** as `"$<op-index>"`
(e.g. add a sketch in op 0, add its entities with `"sketch": "$0"`). Inside
`sketch_add_entity.constraints`, `"$self"` refers to the entity being added.

## 4. Op vocabulary

### Part + general

| op | fields | notes |
|---|---|---|
| `add_feature` | `feature` (full envelope, no `id`), `before` (feature id \| null=append) | placeholder-eligible. With `before=null` AND the rollback bar (§ doc `rollback_after`) NOT at the end, the new feature is inserted **immediately after the bar** and the bar moves to sit after it (SolidWorks behavior); otherwise it appends |
| `edit_feature` | `id`, `params` (patch), `refs` (patch) | patch = present keys replace; `null` deletes an optional key |
| `delete_feature` | `id`, `cascade:bool=false` | without cascade, fails `dependency_exists` if referenced |
| `reorder_feature` | `id`, `before` (feature id \| null=end) | dependency-validated |
| `set_suppressed` | `id`, `value:bool` | |
| `rename` | `id`, `name` | |
| `set_material` | `key` | part docs |
| `set_appearance` | `id` (body key), `color: [r,g,b] 0..1 \| null`, `metallic: 0..1?`, `roughness: 0..1?` | C26 body color + C32-A RealView material; `id` = `"b<ordinal>"` (part body, 1-based AICM body id) or component id (assembly body); `null` color clears to the default. Stored in `appearances: {body_key: ...}` where the value is a bare `[r,g,b]` (color only) OR `{color,metallic,roughness}` (set when `metallic`/`roughness` are supplied — both forms accepted on read and round-trip through save/open). Baked into the AICM BODY chunk: color + a 2-float material trailer appended after `idx` (length-prefixed chunk, so pre-C32 readers skip it and fall back to dielectric `metallic 0 / roughness 0.5`). Slightly-off floats are clamped, not rejected. |
| `set_units` | `system: MMGS\|IPS` | display-only |
| `set_rollback` | `after: feature id \| null` | move the rollback bar (SolidWorks). `after` = the feature the bar sits AFTER (that feature + its prefix compile; everything after is **rolled back** — see worker_rpc §rebuild `rolled_back`). `null` = bar at the end (compile everything). Validates the feature exists (`invalid_op` otherwise). Undo-able. Tree exposes `rollback_after`; delete/reorder keep it valid (deleting the bar feature moves the bar to the previous surviving feature, or `null`) |
| `eq_set` | `name`, `expr` | create-or-update; re-validates dependents |
| `eq_delete` | `name` | fails `eq_in_use` if referenced |

### Sketch (sketch = feature id of a sketch; placeholder-eligible)

| op | fields |
|---|---|
| `sketch_add_entity` | `sketch`, `entity` (no `id`), `constraints:[Constraint]` (inferred relations; may use `"$self"`) |
| `sketch_edit_entity` | `sketch`, `id`, `params` (entity-field patch) |
| `sketch_delete_entity` | `sketch`, `id` (cascades constraints touching it) |
| `sketch_add_constraint` | `sketch`, `constraint` (no `id`) — a dimensional constraint may carry an optional `placement:[u,v]` annotation anchor and `driven:bool`; a `distance` may carry `axis:"h"\|"v"` (point-point only) and accepts a `[point, line]` ref form (perpendicular foot distance) |
| `sketch_delete_constraint` | `sketch`, `id` |
| `sketch_set_dim` | `sketch`, `id`, **any subset of** `value:float`, `expr:string`, `placement:[u,v]\|null`, `driven:bool` (at least one) | update a dimension's driving value/expr, its annotation anchor, and/or its driven flag in one op; `placement:null` clears the anchor |

**Dimensional `distance` refs (C35-overhaul):**
- `[point, point]` — Euclidean distance. Optional `axis:"h"` constrains only `|Δu|`, `axis:"v"` only `|Δv|`; omitted/`null` = aligned (Euclidean).
- `[point, line_entity_id]` — perpendicular distance from the point to the **infinite** line (the line ref is a bare entity id, no sub-handle). `axis` is not allowed here.

**`angle`** is between **two line entity ids** (bare, no sub-handle); the measured angle is reported in `[0, 180)` degrees.

Geometry helpers (defined now; implemented M2 — same envelope, expand to entity edits server-side):
`sketch_trim {sketch, entity, at:[x,y]}` · `sketch_extend {sketch, entity, end}` ·
`sketch_offset {sketch, entities:[ids], distance, side}`

**Sketch mirror / pattern / convert (C53 — reflect / array / project the selection):**

| op | fields |
|---|---|
| `sketch_mirror_entities` | `sketch`, `entities:[id…]` (line/circle/arc), `line` (construction-line entity id = mirror axis) |
| `sketch_pattern_entities` | `sketch`, `mode:"linear"\|"circular"`, `entities:[id…]` (line/circle/arc), + per-mode dims |
| `sketch_convert` | `sketch`, `refs:[Ref…]` where each Ref is `{datum:"front"\|"top"\|"right"}` or `{kind:"face"\|"edge", selector}` (same Ref shape as `sketch_project_ref`) |

- **`sketch_mirror_entities`** reflects each selected entity across the mirror line and appends the
  copies, then adds relations that keep each pair mirrored: per endpoint-pair `symmetric` (refs
  `[orig.handle, copy.handle, line]`) — `p1`/`p2` for a line, `center` for a circle, `center`/`start`/
  `end` for an arc — plus an `equal` (radius) for circles/arcs. Arc copies flip orientation (`ccw`).
- **`sketch_pattern_entities`** (`mode:"linear"`) takes `dir:[dx,dy]` (normalized internally), integer
  `count>=2`, numeric `spacing`; (`mode:"circular"`) takes `center:[x,y]`, integer `count>=2`, numeric
  `angle`, optional `equal_spacing:bool`. `equal_spacing:true` shares `angle` equally across the ring
  (`step = angle/count`, e.g. `360` → full ring); otherwise `angle` is the step BETWEEN consecutive
  instances. It appends `count-1` PLAIN copies (no per-instance relations — a SolidWorks geometry
  pattern); under-defined like fresh geometry.
- **`sketch_convert`** projects each Ref onto the sketch plane via the same math as `sketch_project_ref`
  (planes/planar faces → intersection line; linear edge → line or point; circular edge with axis ∥
  the sketch normal → circle) and appends it as a real sketch entity pinned with a whole-entity `fix`
  relation (reference geometry tracks the model).

All three record the FIRST new entity under `$<index>` and any further new entities under
`$<index>#1`, `$<index>#2`, … (the flat id_map scheme `sketch_offset`/`sketch_trim` use). Supported
entity types for mirror/pattern are **line, circle, arc**; any other selected type raises
`unsupported_entity`. Empty/zero-length `entities`/`refs`, a non-line mirror `line`, `count<2`, or a
zero-length `dir` raise `invalid_op`. A reference that cannot be projected raises `convert_failed` (or
the `unsupported_ref` it bubbles up). Like all op errors these reject the whole batch with no state
change (§2).

> **Amendment (T-BL43, 2026-07-04) — `sketch_convert` projection math upgraded.** The op is now
> implemented in the C++ engine (it was contracted in C53 but had no engine handler — every GUI
> Convert Entities click failed with `unsupported`). Per Hunter's 2026-07-03 direction the
> projection is now true orthographic projection along the sketch normal (OCCT `GeomProjLib`),
> superseding the "same math as `sketch_project_ref`" bullet above for this op:
> a **face** Ref projects ALL its boundary wires (outer + hole loops) as entities — not just a
> plane-intersection line; an **oblique circular edge** projects to an `ellipse`/`ellipse_arc`
> (axis need not be parallel to the sketch normal); an edge (near-)perpendicular to the plane
> collapses to a `point`; unclassifiable curves fall back to a sampled 24-seg polyline. A
> **datum** Ref keeps the intersection-line semantics (a plane has no boundary). New optional
> fields: `construction:bool` (default `false` — real, extrudable geometry, as above) and
> `fix:bool` (default `true` — the whole-entity `fix` pin, as above). Projection is ONE-SHOT:
> converted entities do not update when the source model changes (associative update is a filed
> follow-up). `sketch_project_ref` (the read-only preview RPC) is unchanged; the richer
> read-only companion is `sketch_project_entities` `{doc_id, sketch, refs:[Ref…]}` →
> `{ok, entities (no ids, construction:true), approx, sources, plane_frame}`.

> **Amendment (T-BL49, 2026-07-07) — `sketch_convert` is now ASSOCIATIVE by default.** New
> optional field `associative:bool` (default `true`): each Ref is also recorded on the sketch as
> a persistent external reference (`params.external_refs[]`, id family `x<N>` — see
> doc_schema.md §3.5) owning the entities it produced. On EVERY later rebuild (any mutating
> `doc_apply_ops` batch, and `doc_rebuild`) the engine re-resolves each recorded source and
> re-projects, updating the owned entities **in place under stable entity ids** (user
> constraints/dims attached to converted geometry survive; if a re-projection changes an
> entity's TYPE — e.g. a line collapsing to a point after a plane change — constraints touching
> that entity are cascade-dropped and the `fix` pin re-added, so the document always stays
> valid). Re-resolution uses the **scored selector match with the selectors.md §3 accept
> thresholds** (strict; duplicate-free unique-edge candidate set), NOT the forgiving
> centroid-near pick used at creation: a source that no longer resolves (owner deleted, geometry
> moved past the score threshold, symmetric ambiguity) marks the ref `stale:true` — the
> last-good entities are KEPT, the rebuild NEVER fails because of a stale ref, and the staleness
> surfaces as (a) a feature-status `warning` on the sketch, (b) `sketch_health.external_refs[]`
> rows, and (c) an `external` map (`eid -> {ref, stale}`) on `sketch_enter`/`sketch_solve`/
> `sketch_drag` results (the host styles external entities distinctly and dashes/ambers stale
> ones). If the source later resolves again, the ref self-heals (`stale:false`, geometry
> re-projected). Source growth/shrink (a face gaining/losing boundary edges) appends new ids /
> deletes orphaned tail ids (constraints on deleted entities cascade). `associative:false`
> restores the one-shot copy above. New op **`sketch_break_external_ref`** `{sketch, id:"x<N>",
> delete_entities:bool=false}` severs one link (SolidWorks "break link"): the record is removed
> and the entities stay as ordinary fix-pinned geometry (or are deleted with constraint cascade
> when `delete_entities:true`); an unknown ref id raises `invalid_op`. A ref that cannot resolve
> at CREATION time still fails the op (`convert_failed`/`unsupported_ref`) — staleness only
> applies to later rebuilds.

> **Amendment (T-BL51, 2026-08-03) — external-ref re-projection is part of the edit's undo
> step.** The refresh runs on the batch's working tree BEFORE the undo snapshot is committed,
> so `doc_undo` restores the source geometry AND the projection that belonged to it in one
> step (converted entities never linger at coordinates their source no longer has); `doc_redo`
> restores both again. Entity ids owned by a ref are stable across undo/redo. Neither
> `doc_undo` nor `doc_redo` re-resolves selectors — they replay a self-consistent snapshot.

**Sketch move/copy (C54 — translate or duplicate the selection by a delta):**

| op | fields |
|---|---|
| `sketch_move` | `sketch`, `entities:[id…]` (line/circle/arc), `dx`, `dy` (numeric delta), `copy:bool=false` |

- **`copy:false`** (default) moves the selection **IN PLACE**: each entity's own geometry shifts by
  `(dx, dy)` and its relations ride along — no new entities, the entity count is unchanged. The first
  moved entity id is recorded under `$<index>`.
- **`copy:true`** leaves the originals untouched and **appends one translated copy of each** selected
  entity (a plain-geometry copy, no per-instance relations — like a 2-instance pattern minus the
  original), so the entity count of the selection doubles. The first copy is recorded under
  `$<index>`, the rest under `$<index>#1`, `$<index>#2`, … (the flat id_map scheme).

Supported entity types are **line, circle, arc**; any other selected type raises `unsupported_entity`.
An empty `entities`, an unknown `sketch`, a non-numeric `dx`/`dy`, or a non-bool `copy` raise
`invalid_op`. Like all op errors these reject the whole batch with no state change (§2).

**Sketch fillet / chamfer (round/bevel the corner where two LINE segments meet):**

| op | fields |
|---|---|
| `sketch_fillet` | `sketch`, `e1`, `e2` (the two segment ids sharing/near a corner), `radius` (>0) |
| `sketch_chamfer` | `sketch`, `e1`, `e2`, `distance` (>0 setback along e1), `distance2?` (>0 setback along e2; defaults to `distance` for an equal-leg bevel) |

Both trim `e1` and `e2` back to the corner setback points and insert the connecting geometry,
recording its new entity id under the op placeholder `$<index>`:
- **`sketch_fillet`** inserts a tangent **arc** of `radius` and adds: `coincident` (arc.start ↔ the
  trimmed e1 corner end), `coincident` (arc.end ↔ the trimmed e2 corner end), `tangent(arc, e1)`,
  `tangent(arc, e2)`. (On an orthogonal corner the two tangents are geometrically implied by the
  coincident joins, so the solver may flag one as *redundant* — benign, mirrors SolidWorks.)
- **`sketch_chamfer`** inserts a connecting **line** and adds: `coincident` (line.p1 ↔ trimmed e1
  end), `coincident` (line.p2 ↔ trimmed e2 end).

Both first **drop the original coincident** that joined the two corner endpoints (the sharp corner
is gone) so the new geometry is not pulled back together at solve time.

**Supported:** line–line corners only. A non-line `e1`/`e2` raises `unsupported_entity` (the solver
has no line↔arc / arc↔arc tangent yet — doc_schema.md §3.3). Degenerate input — parallel segments,
no shared corner, or a `radius`/setback too large to fit between the corner and the segments' far
ends — raises `fillet_failed` / `chamfer_failed` with NO mutation (the whole op-batch is rejected
atomically, §2). Non-positive `radius`/`distance`, missing/identical `e1`/`e2` raise `invalid_op`.

### Assembly

| op | fields |
|---|---|
| `asm_insert_component` | `source:{path}`, `transform?` (default identity), `fixed?` (default: first=true), `name?` |
| `asm_edit_component` | `id`, `transform?`, `name?`, `suppressed?`, `fixed?` |
| `asm_delete_component` | `id`, `cascade:bool=false` (mates referencing it) |
| `asm_add_mate` | `mate` (no `id`) |
| `asm_edit_mate` | `id`, `value?`, `align?`, `suppressed?` |
| `asm_delete_mate` | `id` |
| `asm_set_explode` | `steps:[{component, vector:[3], distance}]` (whole-list replace) |

### Copy/paste (Wave 4A/4B)

Move features (with their dependency closure) or loose sketch entities between documents via a
portable, id-free **bundle** (`kind: "aicad.feature_bundle/1"`). `copy_feature` is **read-only**
(no doc mutation) and returns its bundle through the `id_map` side-channel; the paste ops re-id and
append through the same rollback-bar-aware path as `add_feature`.

| op | fields | notes |
|---|---|---|
| `copy_feature` | `id`, `include_dependencies:bool=true` | **read-only**. Packages the feature (and, by default, its full reference closure) into a bundle returned at `id_map["$bundle"]` (and `id_map["$bundle:<op-index>"]` for multi-copy batches). With `include_dependencies=false`, only the root is bundled; every referenced feature is flagged in `external_refs` (datums also in `external_datums` by name). |
| `paste_feature` | `bundle`, `target_doc_id?` | re-ids the bundle and appends it; refs resolved against THIS doc (the batch's target — `target_doc_id` is accepted for host-API symmetry and ignored). Root's new id → `id_map["$<index>"]`; every pasted feature enumerated `id_map["$<index>#k"]` (dependency-first). |
| `paste_feature_as_derived` | `bundle`, `source_doc_id?` | like `paste_feature` + stamps `derived_from:{doc_id,doc_name,feature,source_feature,group}` on each pasted feature (`source_feature` = the live source id the placeholder was bundled from; `group` = a shared id over the whole pasted closure). The paste itself is still a **one-time** resolve; the live re-pull is `refresh_derived` (Wave 4C, below). `source_doc_id` overrides the bundle's `source_doc_id` when the bundle carries none (the host stamps the live **source session doc_id** here so a later refresh can find it). |
| `sketch_paste_entities` | `sketch`, `entities_bundle:{entities:[…], constraints:[…]}`, `offset_x`, `offset_y` | deep-copies the entities shifted by `(offset_x, offset_y)` with fresh `e<N>` ids. Constraints internal to the pasted set are kept (handles re-pointed); constraints referencing any entity OUTSIDE the set (or the origin/model) are **dropped**. First new id → `$<index>`, rest → `$<index>#k`. |

**Bundle format (`aicad.feature_bundle/1`):**

```
{ "kind": "aicad.feature_bundle/1",
  "source_doc_id":   <str|null>,        // provenance (derived-link + cross-doc resolve)
  "source_doc_name": <str|null>,
  "root": "$k",                          // placeholder of the copied (root) feature
  "feature_order": ["$0","$1",…],        // dependency-first build order (deps before dependents)
  "features": { "$k": <feature> },       // id stripped; in-closure refs → placeholders
  "external_refs":   ["f7",…],           // referenced SOURCE ids NOT in the closure
  "external_datums": {"f7":"Datum1"},    // source datum id → name (cross-doc datum reuse)
  "source_ids": {"$0":"f1","$1":"f2"} }  // placeholder → live SOURCE feature id (provenance for
                                         //   derived-link refresh; paste_feature ignores it)
```

Inside a bundled feature: the `id` is removed; feature-id refs in the closure → `$0,$1,…`; sketch
entity ids → `~e0,~e1,…`; constraint ids → `~c0,~c1,…` (handles rewritten in lockstep). Refs that
point OUT of the closure keep their raw source id and are listed in `external_refs`.

**Cross-doc ref resolution at paste** (selectors.md — *never a silent wrong pick*):
- **Topology** selector (face/edge/vertex/body) from source topology → `owner_feature=null` + an
  `external:{doc_id,doc_name,owner_feature}` marker so the compiler dangles for re-pick.
- **Datum** ref → reuse a same-named datum in the target (Top/Front/Right/custom — by `name`),
  else downgrade to a schema-valid plane selector (`plane.front`) + `external:{…,unresolved:true}`.
- **Region** ref → keep the source sketch id (dangling) + `external` marker. **Sketch** refs in the
  closure → deep-copied to fresh ids with internal constraints preserved.
- `_FEAT_LIST` slots (pattern/mirror `features`) keep only in-closure (re-id'd) members; external
  feature ids are dropped from the list (they cannot satisfy the schema precede-rule).

A bundle whose `kind`/`feature_order`/`features`/`root` is missing or malformed raises `invalid_op`
with no state change (§2).

### Derived live relink (Wave 4C)

`refresh_derived` re-pulls a derived feature's definition from its **live source document** —
SolidWorks derived-part behavior, where editing the source updates the derived copy. It is a
**session-level operation** (not a pure doc op in this table): it needs the live session registry to
reach the source document, so the worker dispatches it to `engine.doc.session.refresh_derived`
(alongside `solve_mates` / `config_*` / `revert`), NOT through `apply_ops`. It is **on-demand** (a
"Rebuild derived" the host calls, e.g. on re-open) rather than an auto-cascade across sessions —
honest behavior with no cross-session rebuild-ordering or reentrancy hazard.

| call | params | notes |
|---|---|---|
| `refresh_derived` | `doc_id`, `id?` | With `id`, refresh only the derived group that feature `id` belongs to (a non-derived `id` → `invalid_op`); without `id`, refresh **every** derived group. **Mutating**: one undo snapshot + revision bump (a single Ctrl+Z reverts a refresh), then rebuild. |

**Flow** (per group — a group = the features pasted by one `paste_feature_as_derived` call, sharing
`derived_from.group`):
1. Resolve the live source session from `derived_from.doc_id` (matched against either the session
   registry key **or** the document's internal `doc["id"]`, so it works regardless of which the host
   stamped).
2. Recover the group's source **closure root** (the member whose `source_feature` closure in the
   source doc covers the group) and re-bundle it with the SAME `build_feature_bundle` copy path the
   original `copy_feature` used — so cross-doc refs re-resolve exactly as the paste did (the rules
   above).
3. Splice the fresh bundle over the existing derived features **in place**: each member matched to a
   bundle placeholder by its recorded `source_feature` keeps its **id + tree position**, and its
   params/refs/type are replaced (its user-facing `name` is preserved). New source-closure features
   are appended (also stamped derived); members whose source feature vanished are marked stale.

**Response**: the normal ops-response plus a `derived:{groups:[{group, status, …}]}` block; per-group
`status` ∈ `refreshed` (with `{refreshed:[fid…], added:[fid…], stale:[fid…]}`) · `source_doc_closed`
· `source_feature_deleted` · `refresh_failed` (with `{stale:[fid…]}`).

**Failure handling** (selectors.md — *never silently wrong*, never crash):
- **Source doc closed/absent** (`get_session` miss) → the group is left at its last-resolved
  definition and every member stamped `derived_stale:{reason:"source_doc_closed", doc_id, doc_name}`.
- **Source feature deleted** (closure root gone) → the group is left as-is and stamped
  `derived_stale:{reason:"source_feature_deleted", …}`. When only PART of the closure was deleted,
  surviving members refresh and the orphaned member is individually marked stale.
- A refresh that would yield an invalid document **rolls back** (the pushed undo snapshot is popped,
  the prior doc restored) and raises `invalid_op` — no half-applied refresh commits.
- A pure no-op refresh (e.g. every group already stale and unchanged) drops its undo snapshot and
  does **not** bump the revision.

## 5. Error codes (RPC error envelope `{code, message, detail}`)

`unknown_doc` · `stale_revision {detail.current_revision}` · `invalid_op {detail.index,
detail.reason}` · `dependency_exists {detail.dependents}` · `eq_in_use {detail.referencing}` ·
`io_error` · `unsupported` · `internal`

Sketch-geometry op codes (all carry `{detail.index, detail.reason}`): `unsupported_entity`
(sketch_trim/extend/offset/fillet/chamfer/mirror/pattern hit a non-supported entity type) ·
`extend_no_target` (sketch_extend found nothing ahead) · `fillet_failed` / `chamfer_failed`
(degenerate corner: parallel, no corner, or radius/setback too large) · `mirror_failed`
(degenerate mirror line) · `convert_failed` (sketch_convert could not project a ref) ·
`unsupported_ref` (sketch_convert/project: parallel plane or unprojectable edge). Like all op errors
these reject the whole batch with no state change (§2).

`stale_revision` is the optimistic-concurrency signal: the GUI must resync (`doc_tree`) and replay
user intent. Rebuild failures are NOT errors (see §2.3).

## 6. Multi-user events & history (append-only, 2026-07-16)

### 6.1 `doc.changed` user field

Every `doc.changed` fanout payload gains:

```json
{"event":"doc.changed","data":{
  "doc_id":"…", "revision":N, "source_client":1, "source_kind":"gui",
  "method":"doc_apply_ops",
  "user":{"id":"…","display":"…"},
  "aicm_path":"…", "tree":{…}, …
}}
```

`user.id` / `user.display` are empty strings when the writer did not declare identity on hello.

### 6.2 `doc.history_cleared`

After a successful **gui** `doc_save`, hostd clears that doc's undo/redo history and fans:

```json
{"event":"doc.history_cleared","data":{"doc_id":"…"}}
```

to **all** subscribers (including the saver). GUIs grey local undo/redo until a subsequent edit.

### 6.3 `doc_clear_history` RPC

Engine method `{doc_id}` → `{ok:true, doc_id}`: empties undo_stack + redo_stack and deletes
`<stateDir>/hostd_undo/<instance-key>/<doc_id>/`. No revision bump. See worker_rpc §5.2.

### 6.4 `undo_denied` error code

`doc_undo` / `doc_redo` from mcp/agent clients return
`{"code":"undo_denied","message":"agents use doc_checkpoint/doc_revert or PLM rollback"}`
unless `CHUDWARE_UNDO_AGENTS=1`. Added to the error envelope vocabulary of §5.


## 7. Display tessellation quality (append-only, 2026-09-04)

Every open document carries a **display quality** tier that selects the BRepMesh deflections its
AICM display bodies are meshed at. It is a **view** setting: it never enters the document JSON,
never bumps the revision, never dirties the doc, and is never undoable — the same class as the
assembly explode state (§ `doc_set_explode_state`).

| tier | linear deflection (mm) | angular deflection (rad) |
|---|---|---|
| `draft` | `clamp(bbox_diag * 6.0e-4, 0.02, 1.0)` | `0.35` |
| `standard` | `clamp(bbox_diag * 3.0e-4, 0.01, 0.5)` | `0.20` |
| `fine` | `clamp(bbox_diag * 1.5e-4, 0.005, 0.25)` | `0.10` |

A shape with no measurable bounding box falls back to `0.05` / `0.025` / `0.0125` mm respectively.
The tessellator's existing display clamp still caps angular deflection at `0.20` rad, so `draft`
and `standard` differ in chord tolerance only.

Default tier: **`fine`**, overridable process-wide by `CHUDWARE_DISPLAY_QUALITY=draft|standard|fine`
(an unrecognised value falls back to `fine`), so headless renders and the GUI agree.

Changing the tier invalidates the document's compile cache and its per-body mesh cache, so every
body re-meshes at the new deflections. The RPC is `doc_set_display_quality` (worker_rpc §19).

## 8. `asm_edit_component` transform semantics (append-only, 2026-09-04)

Clarifies the `asm_edit_component` row of §Assembly. `transform` means one of two things,
depending on who owns the component's pose:

| component | `transform` is | after the rebuild |
|---|---|---|
| `fixed:true` | the **authored** pose | kept verbatim (a fixed frame contributes no solver variables) |
| unfixed, no active mate references it | the **authored** pose | kept verbatim (nothing pulls it) |
| unfixed, referenced by a non-suppressed mate | the mate solve's **seed** | replaced by the mate solution; free DOFs keep the seed |

Every `doc_apply_ops` batch on an assembly re-solves the mates and writes the solved transforms
back into `components[]`, so the third row is not an error path — it is the mate being
authoritative. To move a mated component, drive its mate (`asm_edit_mate value`), or suppress /
delete the mate, or set `fixed:true` in the same batch as the `transform`.

An `asm_edit_component` carrying `transform` (or `fixed:true`) invalidates that component's
warm-start entry, so the authored pose — not the previous solve — seeds the solve triggered by
its own batch. Before this, such an edit was silently discarded while still reporting `ok`.
That invalidation is narrowed to the components whose pose the document actually authors — a
`fixed` component (already fixed, or promoted by the same op) and an unfixed component that no
non-suppressed mate references; a mated, unfixed component keeps its warm seed, so its authored
`transform` is an engine-level no-op (re-seeding a mate solve from an off-solution pose converges
to a different pose rather than back onto the mate solution).

## 9. `feature_status[].data` and the `heal` feature (append-only, 2026-09-05)

`doc_apply_ops` → `rebuild.feature_status[]` gains one field:

| field | type | meaning |
|---|---|---|
| `data` | object \| null | structured per-feature RESULT record; `null` for every feature that measures nothing |

`data` is additive and nullable, so every existing reader is unaffected. It is NOT a second
error channel: `ok`/`error`/`warning` keep their meanings, and `data` only carries measurements
a caller would otherwise have to re-derive with a follow-up query.

Its first producer is the new `heal` feature (`add_feature` with `feature.type = "heal"`, params
in [feature_params.md](feature_params.md#heal)), which runs OCCT ShapeFix over a body and
replaces it in place:

```json
{"id": "f3", "ok": true, "warning": null,
 "data": {"fixed": true,
          "before": {"valid": false, "issues": ["bad_orientation_of_subshape"]},
          "after":  {"valid": true,  "issues": []},
          "volume_before": 24000.0, "volume_after": 24000.0,
          "tolerance": 1e-07, "fix_small_edges": false}}
```

A repair is only real if it is part of the feature tree: `heal` is recorded, re-runs on every
rebuild, and its before/after BRepCheck verdict is the evidence. Repairing a body outside the
tree (a BREP/STL round-trip, a boolean with itself) is not a repair — nothing downstream can
tell what was changed, and the next rebuild loses it.

## 10. Carried assembly pose seeds and angle hinge lines (2026-09-08)

When an `asm_edit_component` authors a grounded component's transform, a floating
mate-connected island whose only grounded boundary is that component has its warm
poses transported by `T_new * inverse(T_old)`. This composes the complete rigid
transform with every carried component's existing world placement; it preserves
unconstrained wheel spin and axial station instead of asking a least-squares solve
to choose them again. The components remain floating and all their mate equations
are still solved. Islands with multiple grounded boundaries are linkage networks,
not rigidly carried children, and are not transported this way. Assembly operation
replies add `pose_transport: {carried_component_id: grounded_component_id}`.

Angle `value` objects may define a hinge line with `axis:[x,y,z]` (existing alias:
`reference_axis`) and `axis_point:[x,y,z]`, both in the first reference component's
local frame. An authored axis is normalized, never flattened to a different axis.
The scalar mate alone still constrains only an angle. A motion sweep additionally
requires an identifiable axis line, as specified in `worker_rpc.md`.
