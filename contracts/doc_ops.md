# Contract: Document Ops — v1.2 (v2 amendments below)

> The closed vocabulary of document mutations. The GUI submits these; **the M9 AI modeler emits
> exactly these as its tool calls.** Design once, freeze here. Changes require a version bump.
>
> **v1.1 (C35-overhaul):** additive — new `set_rollback` op (rollback bar); `add_feature` mid-bar
> insert semantics; `sketch_set_dim` extended to update any subset of `value`/`expr`/`placement`/
> `driven`; dimensional `distance` gains an optional `axis` and a point-line ref form; dimensional
> constraints gain an optional `placement` annotation anchor. No existing field changed shape.
>
> **v1.2:** additive — `simplify_history` provides conservative, explicitly requested history
> cleanup through `doc_apply_ops`. Existing op meanings are unchanged.
>
> **v2.1 amendment:** hostd document-scoped human-edit attribution and persistence barriers
> are specified below. The operation vocabulary is unchanged.

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
- `with_tess` is an optional request boolean (default `true`). When `false`, part and
  assembly operation responses omit `tess` and do not publish a response mesh.
  Document edits, mate solving, undo/redo state, `rebuild` diagnostics, `owner_map`,
  and applicable non-mesh result fields are unchanged. Part compilation may still
  construct its internal geometry/display cache. A later normal display request
  renders the committed state; this flag does not suppress geometry validation.
- `sketch_session_id` is an optional request integer (the `session_id` of `sketch_enter`;
  `doc_undo`/`doc_redo` accept it too). While it owns the active sketch and the batch contains
  only `sketch_*` ops on that sketch, the history is compiled only up to the sketch:
  every later feature reports `rolled_back:true`, `rebuild.doc_status.rolled_back_to`
  names the sketch, and `exact` is omitted. The document, its undo history and `rollback_after`
  are unchanged (the rollback is transient); every other reader still rebuilds the complete
  history, and `sketch_exit` rebuilds it once (new revision, `auto_rollback` if the edits broke a
  later feature). `sketch_convert` sources and associative `external_refs` resolve against the
  history through the edited sketch; the refs of later sketches are refreshed at `sketch_exit`.
  Documents with `sketch3d` `external_refs` always rebuild completely.
- `with_exact` is an optional request boolean (default `true`); `false` omits the `exact`
  statistics from the reply (interactive clients that never read them).
- `preview_through` is an optional request feature id for property-manager previews. A batch that
  only edits that feature (`edit_feature`) and is not the last feature compiles the history only
  up to it, with the same reply shape and transient-rollback rules as `sketch_session_id`; the
  client's final `edit_feature` (without the key) rebuilds the complete history, with the usual
  `auto_rollback` handling.

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
| `simplify_history` | `features:[feature id…]?` | remove only currently proven-safe candidates; omitted selects all eligible, empty selects none. See below |
| `reorder_feature` | `id`, `before` (feature id \| null=end) | dependency-validated |
| `set_suppressed` | `id`, `value:bool` | |
| `rename` | `id`, `name` | |
| `set_material` | `key` | part docs |
| `set_appearance` | `id` (body key), `color: [r,g,b] 0..1 \| null`, `metallic: 0..1?`, `roughness: 0..1?` | C26 body color + C32-A RealView material; `id` = `"b<ordinal>"` (part body, 1-based AICM body id) or component id (assembly body); `null` color clears to the default. Stored in `appearances: {body_key: ...}` where the value is a bare `[r,g,b]` (color only) OR `{color,metallic,roughness}` (set when `metallic`/`roughness` are supplied — both forms accepted on read and round-trip through save/open). Baked into the AICM BODY chunk: color + a 2-float material trailer appended after `idx` (length-prefixed chunk, so pre-C32 readers skip it and fall back to dielectric `metallic 0 / roughness 0.5`). Slightly-off floats are clamped, not rejected. |
| `set_units` | `system: MMGS\|IPS` | display-only |
| `set_rollback` | `after: feature id \| null` | move the rollback bar (SolidWorks). `after` = the feature the bar sits AFTER (that feature + its prefix compile; everything after is **rolled back** — see worker_rpc §rebuild `rolled_back`). `null` = bar at the end (compile everything). Validates the feature exists (`invalid_op` otherwise). Undo-able. Tree exposes `rollback_after`; delete/reorder keep it valid (deleting the bar feature moves the bar to the previous surviving feature, or `null`) |
| `eq_set` | `name`, `expr` | create-or-update; re-validates dependents |
| `eq_delete` | `name` | fails `eq_in_use` if referenced |

**History cleanup (`simplify_history`):**

`eval_model_history({doc_id})` is a read-only preview of the document-history rules. Its revision,
findings, safe fixes and limitations are not a geometric or engineering certification. To apply
cleanup, submit `simplify_history` in a normal `doc_apply_ops` batch with the preview revision as
`base_revision`. The native engine reanalyzes eligibility under the document lock; a stale base
revision is rejected rather than applying an outdated preview. No separate mutation endpoint is
provided.

The proof is conservative: candidates are active unreferenced datum features (plane, axis, point,
or coordinate system), or literal exactly-zero in-place transforms that are neither referenced
internally nor externally observable as final output. Internal, metadata, configuration and live
consumer references are guarded; saved/public documents are blocked when persisted external
consumers cannot be established. Suppressed, configuration-driven or rolled-back features and
other uncertain cases are not automatically removed. In particular, face ownership and equal volume
do not prove that a feature is unused. Findings not proven safe remain advisory.

An explicit `features` list must consist entirely of currently eligible feature IDs; an unknown or
ineligible ID rejects the entire batch as `invalid_op`, without changing state. Omission requests all
currently eligible candidates; an empty list requests none. If cleanup makes no change, it does not
by itself dirty the document, increment the revision, or add an undo entry. Otherwise cleanup obeys
the same atomic batch and whole-batch undo/redo rules as other document operations; other operations
in the batch retain their normal effects.

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

**`rotate_point`** is a signed angular driving constraint with refs
`[source_point, target_point, pivot_point]` and `value:{value,expr}` in degrees. It enforces
`target = pivot + R(value) * (source - pivot)`, including when the source is at the pivot.
The measured value is the signed principal angle in degrees; it is unavailable when either
point is at the pivot, where geometric angle is undefined.
An optional `angle_ref` on a follower names an independent explicit driving `rotate_point`
constraint in the same sketch. Its stored value mirrors the master; edit the master with
`sketch_set_dim` (follower value/expression/driven edits are rejected). Deleting a master
cascades to its followers, and the op report lists all removed ids. Overview constraints
expose `angle_ref`; `sketch_health.angle_links` lists the links.

Geometry helpers (defined now; implemented M2 — same envelope, expand to entity edits server-side):
`sketch_trim {sketch, entity, at:[x,y]}` · `sketch_extend {sketch, entity, end}` ·
`sketch_offset {sketch, entities:[ids], distance, side}`
`sketch_offset` accepts an ordered connected line/arc chain, or one circle, a positive distance
(number or `{value,expr}`) and a side point `[u,v]`. The resulting spans retain driving distance
and parallel or concentric relations; unsupported or unjoinable offsets fail the batch.

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
- **`sketch_pattern_entities`** (`mode:"linear"`) takes `dir:[dx,dy]`, `count>=2` and a positive
  `spacing`; circular mode takes `center:[x,y]`, `count>=2`, `angle` and optional `equal_spacing`.
  Count includes the source. With `equal_spacing:true`, the angular step is `angle/count`; otherwise
  it is `angle` per step. An omitted flag defaults to equal spacing for a 360-degree sweep. Cases
  that would stack copies, including a circle centered on the pattern center, are rejected.
  Copies retain parametric spacing and source relations; dimensional inputs accept numbers or
  `{value,expr}`.
- **`sketch_convert`** projects each Ref onto the sketch plane via the same math as `sketch_project_ref`
  (planes/planar faces → intersection line; linear edge → line or point; circular edge with axis ∥
  the sketch normal → circle) and appends it as construction geometry by default, pinned with a
  whole-entity `fix` relation. Set `construction:false` to include the projection in profiles.

All three record the FIRST new entity under `$<index>` and any further new entities under
`$<index>#1`, `$<index>#2`, … (the flat id_map scheme `sketch_offset`/`sketch_trim` use). Supported
entity types for mirror/pattern are **line, circle, arc**; any other selected type raises
`unsupported_entity`. Empty/zero-length `entities`/`refs`, a non-line mirror `line`, `count<2`, or a
zero-length `dir` raise `invalid_op`. A reference that cannot be projected raises `convert_failed` (or
the `unsupported_ref` it bubbles up). Like all op errors these reject the whole batch with no state
change (§2).

> **Amendment (2026-07-04) — `sketch_convert` projection math upgraded.** The op is now
> implemented in the C++ engine (it was contracted above but had no engine handler — every GUI
> Convert Entities click failed with `unsupported`). Per a 2026-07-03 design decision the
> projection is now true orthographic projection along the sketch normal (OCCT `GeomProjLib`),
> superseding the "same math as `sketch_project_ref`" bullet above for this op:
> a **face** Ref projects ALL its boundary wires (outer + hole loops) as entities — not just a
> plane-intersection line; an **oblique circular edge** projects to an `ellipse`/`ellipse_arc`
> (axis need not be parallel to the sketch normal); an edge (near-)perpendicular to the plane
> collapses to a `point`; unclassifiable curves fall back to a sampled 24-seg polyline. A
> **datum** Ref keeps the intersection-line semantics (a plane has no boundary). New optional
> fields: `construction:bool` (default `true`; set `false` for real, extrudable geometry) and
> `fix:bool` (default `true` — the whole-entity `fix` pin, as above). Projection is ONE-SHOT:
> converted entities do not update when the source model changes (associative update is a filed
> follow-up). `sketch_project_ref` (the read-only preview RPC) is unchanged; the richer
> read-only companion is `sketch_project_entities` `{doc_id, sketch, refs:[Ref…]}` →
> `{ok, entities (no ids, construction:true), approx, sources, plane_frame}`.

> **Amendment (2026-07-07) — `sketch_convert` is now ASSOCIATIVE by default.** New
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

> **Amendment (2026-08-03) — external-ref re-projection is part of the edit's undo
> step.** The refresh runs on the batch's working tree BEFORE the undo snapshot is committed,
> so `doc_undo` restores the source geometry AND the projection that belonged to it in one
> step (converted entities never linger at coordinates their source no longer has); `doc_redo`
> restores both again. Entity ids owned by a ref are stable across undo/redo. Neither
> `doc_undo` nor `doc_redo` re-resolves selectors — they replay a self-consistent snapshot.

**Sketch move/copy (C54 — translate or duplicate the selection by a delta):**

| op | fields |
|---|---|
| `sketch_move` | `sketch`, `entities:[id…]` (line/circle/arc), `dx`, `dy` (numeric delta), `copy:bool=false` |

- **`copy:false`** moves the selection in place and keeps its entity ids; construction
  references anchor each source entity's pose while leaving line length and circle/arc radius
  editable. Their individual pose anchors can still conflict with edits that move joints between
  selected entities or change a line/arc's source orientation or arc sweep.
- **`copy:true`** appends a copy linked to the original by driving offsets. The first copy is
  recorded under `$<index>`, the rest under `$<index>#1`, `$<index>#2`, … .
  `sketch_rotate` similarly accepts `cx`, `cy`, `angle` and `copy`, and links its results by
  parametric angular relations. Numeric inputs may be `{value,expr}`.
  Each move axis has one signed driving dimension shared by all selected handles. Zero and
  sign changes are supported without changing the direction branch.

Supported entity types are **line, circle, arc**; any other selected type raises `unsupported_entity`.
An empty `entities`, an unknown `sketch`, a non-numeric `dx`/`dy`, or a non-bool `copy` raise
`invalid_op`. Like all op errors these reject the whole batch with no state change (§2).

**Sketch fillet / chamfer (round/bevel the corner where two LINE segments meet):**

| op | fields |
|---|---|
| `sketch_fillet` | `sketch`, `e1`, `e2` (the two segment ids sharing/near a corner), `radius` |
| `sketch_chamfer` | `sketch`, `e1`, `e2`, `distance` (>0 setback along e1), `distance2?` (>0 setback along e2; defaults to `distance` for an equal-leg bevel) |

Both trim `e1` and `e2` back to the corner setback points and insert the connecting geometry,
recording its new entity id under the op placeholder `$<index>`:
- **`sketch_fillet`** inserts a tangent arc, joined to both trimmed lines, and a driving radius.
  Out-of-range numeric and expression radii are clamped and reported in `op_reports` as
  `requested_radius` and `applied_radius`. A clamped expression is retained inside `min`/`max`
  with bounds computed from the corner at operation time; the report exposes those snapshot
  bounds. Later geometry changes can invalidate the frozen upper bound. In-range expressions
  remain unchanged.
- **`sketch_chamfer`** inserts a connecting line and coincident joins, plus a construction
  intersection point and driving setback dimensions for both legs.

Both first **drop the original coincident** that joined the two corner endpoints (the sharp corner
is gone) so the new geometry is not pulled back together at solve time.

**Supported:** line–line corners only. Non-line inputs, unsolvable constraints and degenerate
corners reject the entire batch. Chamfer setbacks must be positive and fit on the source lines.

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
| `sketch3d_add_entities` | `sketch`, `entities:[{type, p/p1/p2/pm/points, construction?, id?}, …]` | **atomic append** to a `sketch3d` feature inside the engine (no read-modify-write). `e<N>` minted per sketch for entities without an `id`; first id → `$<index>`, rest → `$<index>#k`. Malformed element / duplicate id → `invalid_op` naming `entities[i]`. See `sketch3d_gui_contract.md` §4.1b. |
| `sketch3d_add_constraints` | `sketch`, `constraints:[{type, refs, value?, axis?/dir?/plane?, …}, …]` | atomic append of constraints to a `sketch3d`. `c<N>` minted per sketch; numeric `value` stored as `{value, expr:null}`; `$<i>` / `$<i>.p2` refs name entities minted earlier in the batch. Ids → `$<index>`, `$<index>#k`. |
| `sketch3d_add_external_ref` | `sketch`, `ref:{source:{kind:"vertex", selector}}` | atomic append of a model-vertex external reference to a `sketch3d`. `x<N>` minted doc-wide (usable as a point handle); resolved by the batch's external-ref pass. Id → `$<index>`. |

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
Nonbinding snapshot saves (`preserve_path:true`, §24) are not human Save
operations and do not clear history or emit `doc.history_cleared`.


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
body re-meshes at the new deflections. The RPC is `doc_set_display_quality` (worker_rpc §19);
setting the tier a document already has keeps its compile and caches.

The desktop GUI persists its choice (Settings → General → Display quality, `display_quality` in
config.json). Native opens pass it as `doc_open.display_quality` (2026-11-07), so the session
starts at that tier and a build cache saved at it is served warm. Any other part/assembly doc
that is not at the saved tier (new docs, imports, reused sessions) gets it via
`doc_set_display_quality` (`origin: "system"`) — on restart and for docs opened afterwards — so
the saved tier is not lost to the engine's `fine` default. A saved `fine` pushes nothing.

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

## 11. Explicit line angle mates are revolute joints (2026-09-08)

Superseding the scalar-only statement above for explicit lines: an angle mate
carrying both `axis` (or `reference_axis`) and `axis_point` binds a virtual pin to
the two authored component frames. No cylindrical face or bore is required.
Five structural residuals preserve the three coordinates of the pin station and
align its two axes; the signed angular residual prescribes the remaining rotation.
Binding uses document placements, never warm-start poses, so sweeps cannot drift
axially or rebind their pivot. Points may be zero and are not normalized.
Without `axis_point`, existing single-angular-residual semantics are unchanged.

For an explicit-line angle driver, motion trajectory `dof` reports mechanism
mobility including the driven coordinate (one for a single grounded revolute).
`residual_dof` reports the actual solver nullity with the angle prescribed (zero
for that mechanism). Mate-solver DOF remains the actual residual nullity.

## 12. Nested native component geometry (2026-09-08)

Loading a `.step+` component dispatches on the contained schema: part documents
compile their features, assembly documents recursively resolve components and
world-place them inside the child's local frame before the parent's pose is applied.
Relative references resolve against their containing file. Cycles and missing or
solid-free descendants fail explicitly; they cannot contribute a partial compound
to an export. Native assembly results are not cached solely by the parent mtime.
Detached `doc_open` imports rebase relative component paths before clearing the
source save destination. Export metadata and the explicit MCP resume opt-in are
specified in `worker_rpc.md` §31.

## 13. MCP-authored revolute lines and solve diagnostics (2026-09-08)

At the MCP `asm_mate` boundary, an angle `value` object carrying `axis` (or
`reference_axis`) and `axis_point` supplies a WORLD-coordinate line. The tool
converts it once into the first reference component's local frame before
`asm_add_mate`; the native document schema remains local as specified above.
Save/close/open preserves that stored line, including non-identity component
placements.

If either selected direction is parallel to an explicit pin line, face normals
cannot measure the joint's phase. The revolute uses a deterministic perpendicular
datum attached to the first component and the two authored body orientations as
zero angle. It still contributes all six residuals; neither a rear bore nor a
different face selector is required. Scalar-only angles retain their existing
semantics and refuse an unmeasurable reference axis.

A failed residual builder marks its mate `unresolved`, preserves the diagnostic in
the mate's `reason`, and makes the native solve nonconverged. MCP `solve_mates`
returns `ok:false`, `reason:"mate_unresolved"`, and an error naming the mate and
diagnostic, rather than reporting a vacuously converged empty residual system.

For explicit-line angles, `doc_solve_mates` rebuild results, MCP `solve_mates`,
and `describe_assembly` now expose `residual_dof` beside mechanism `dof`, matching
motion frames. A grounded revolute has `dof:1` and `residual_dof:0` at its prescribed
angle; the underlying mate solver continues to report actual residual nullity.

## 14. Component suppression and transform ownership (2026-09-08)

`set_suppressed {id, value}` addresses feature ids on part documents and component
ids on assembly documents. `value:true` suppresses; `value:false` restores.
Unknown or wrong-kind ids produce a named refusal identifying the document kind,
not the former unqualified `no feature` error.

An assembly `add_feature` of type `transform` addresses `body:<component-id>` and
is applied as a placement mutation, composing a world-space rigid delta with the
current placement. It never stores an inert part feature in the assembly tree.
`copy:true` alone requests a new component. Missing components and mate-owned
unfixed poses refuse atomically. See `feature_params.md` for body-reference and
rotation semantics.

## 15. Component source replacement

`asm_edit_component {id, source:{path|embedded}}` replaces the component source
without changing its id, authored transform, fixed state or mate references.
The source object uses the same validation as `asm_insert_component`; malformed
sources refuse the operation atomically. The next rebuild and saved/exported
geometry use the replacement. Existing mate references are retained, not
automatically remapped to unrelated replacement topology; inspect solve status
after replacement.

## 16. Passive explicit-line hinges

A native `hinge` mate may specify `value.axis` (or `reference_axis`) and
`value.axis_point` together. These coordinates are local to the first reference
component. The second reference binds through authored component placements, not
through the current warm-start pose. Five structural constraints preserve the
pin station and axis while leaving its angular coordinate free; a scalar `value`
does not turn the passive hinge into a prescribed angle. Existing angular limits
remain active. Incomplete or malformed explicit pin metadata refuses. Geometric
hinges and prescribed angle-mate behavior retain their existing semantics.

## 17. Oriented hinge alignment (2026-09-11)

A hinge preserves its authored pin direction, not merely an unoriented line.
Geometric hinges honor `align:"aligned"` or `"anti"`; explicit virtual pins bind
the same oriented reference line on both bodies independently of selector
alignment. Antiparallel axes cannot satisfy an aligned hinge. Direction residuals
are dimensionless normalized-vector chord components; their rank is two at a
valid alignment. Coincident pin origins and the free angular coordinate remain
unchanged. Contradictory orientation constraints refuse convergence rather than
accepting a disconnected half-turn branch. No solve tolerance is widened.

## 18. Oriented coaxial and prescribed pin alignment (2026-09-11)

The oriented-axis rule also applies to concentric mates, coincident axis/axis
mates, and explicit-line angle mates. Concentric and coincident axis/axis mates
honor `align` while retaining free axial translation and rotation. An explicit
angle pin retains its authored direction as well as its prescribed phase and
coincident origin; satisfying only the phase with a reversed pin is invalid.
Its existing phase-alignment convention is unchanged. Point coincidence and
ordinary direction-only angle mates keep their existing constraints.

## 19. Body-scoped physical material identity (2026-09-11)

`set_material` accepts an optional `id:"bN"` on part documents, using the
same 1-based body identifiers as `set_appearance`. A non-null `key` with an
`id` stores `body_materials[id] = {"key": key}` without replacing the part's
`material` default. A null or omitted `key` removes that body's override,
restoring inheritance from the default. The empty override map is removed.
Body override keys must have canonical positive ordinals (`b1`, not `b0`
or `b01`), and values must be objects containing a string `key`.

Without `id`, the operation replaces the whole-part material and clears all
body overrides. This preserves whole-part assignment semantics when the part
previously had mixed materials. `set_appearance` does not alter either physical
material field. The fields are exposed by the document tree and persist through
native save/open.

The MCP `set_material(body=...)` tool checks the live body inventory with a
read-only query before assigning an override; an absent body refuses without
changing material, appearance, or document revision. Body identifiers refer to
the current part inventory, not an assembly's source-component material.
These identity fields do not change the explicit material/density inputs of
mass or FEA calls.

## 20. Material catalog and standard fastener limits (2026-09-11)

Material identities may name density-only or direction-dependent registry
records. A mass query with an explicit material name resolves its density
without requiring isotropic stiffness or yield data; an explicit density
still takes precedence. Isotropic FEA refuses records marked with a
non-isotropic or incomplete constitutive model before meshing. Registry
appearances take precedence over family heuristics, including natural POM.
These rules do not make saved body material identities implicit analysis inputs.

Metric `hex_bolt` geometry uses tabulated ISO 4017 nominal head heights,
including 7.5 mm for M12. `options.standard` accepts `ISO4017` or `ISO 4017`
for this family; other standards, unknown options, mechanical-property
claims such as `property_class`, and non-object options refuse rather than
silently generating a different request. The native geometry is a schematic
thread envelope, not a full tolerance or thread-form model. SAE hex-bolt
dimensions remain explicitly approximate. A class 10.9 material identity
does not certify purchased fastener mechanical properties.

## 21. Body-scoped assembly geometry queries (2026-09-11)

`doc_query` with a positive 1-based `query.body_id` selects a world-placed
assembly body in the same order as the un-exploded assembly/export inventory.
It does not compile the assembly as an empty part or return local source
coordinates. An out-of-range body refuses; it never falls back to querying
the whole assembly. Unscoped queries retain whole-document behavior.
This allows `interface_check` to compare real placed component faces.

The read-only `doc_query` option `fastener_geometry:true` adds finite B-rep
evidence to face matches without changing selector identity. Use `body_id` to
scope match and adjacent-face `topo` IDs to one world-placed body; use `count`
or `index` to bound the returned matches. `cylinder` contains the world
`axis_point`, unit `axis`, `radius`, `kind`, ordered axis points `axial_start`
and `axial_end`, `complete_cylinder`, `status`, and two corresponding `ends`.
End records report adjacent face `sid`/`topo`/`type`, `adjacency_complete`, and
`classification` (`open_to_exterior`, `closed_bottom`, `coaxial_transition`,
or `unknown`). Only proven full or simple partial circular trims receive
extents; unsupported trims have null extents and completeness. Opening
classification is conservative and requires an extreme body plane, not just
an unobstructed axis. A planar face's `bearing_annulus` is populated only for
an exactly bounded concentric two-circle annulus; it contains world `center`,
oriented `normal`, `inner_radius`, `outer_radius`, `adjacent_faces`, and
`adjacency_complete`. Null and unknown mean evidence is unavailable, not
that the feature is absent.

## 22. Selected-face visual finishes (2026-09-12)

On a source part, `set_appearance` additionally accepts a nonempty `faces`
array of stable `Fxxxxxx` SIDs or face selector objects containing `kind:"face"`
and `sid`. `id:"bN"` selects the source body. New assignments require explicit
RGB `color`; optional `metallic` and `roughness` inherit the body's values when
omitted. This changes appearance only, never document or body physical material.

Selections resolve uniquely against the final candidate geometry before the
operation batch commits. Unknown or ambiguous SIDs reject the batch. Ordinal-only
and fuzzy matching are not supported. Records persist as
`face_appearances[bN][SID] = {selector, appearance}`, where `selector` is the
canonical geometric fingerprint without owner or ordinal hints.
Cylindrical faces use the same geometry-derived bore/boss classification in
queries, viewport picks and appearance validation, so their SIDs agree.
Rendering requires both a unique SID and the saved fingerprint; stale records
remain inspectable but do not paint another face. `color:null` clears only the
listed stored SIDs, including records whose face or body no longer exists.

The registered `set_material` tool exposes the same visual-only selection via
`faces`, appended after its existing parameters. Use an appearance dict or RGB
list, not a physical material name; `{"color":null}` clears selected overrides.
Default `get_part`/`doc_tree` exposes the persisted records. Native save/open,
compiled AICM, direct rendering and nested source-part assemblies preserve them.
Desktop APRG rendering uses color, metallic and roughness; the existing software
rasterizer uses region colors without claiming equivalent PBR shading.

## 23. Cached native dry-run diagnostics (2026-09-12)

`doc_dry_run_ops(doc_id, ops)` compiles a candidate in unregistered scratch
state seeded from the live source's compile cache and relative-path context.
It copies mutable cache indexes while retaining immutable cached payloads.
Neither success nor failure may change the source tree, revision, geometry,
undo history or compile cache, or notify dependent live documents.

The response retains its `ok`/`error` fields and includes `source_revision`.
When candidate compilation is reached it also returns the actual `rebuild`
diagnostics, including feature status, document status and cache statistics.
Callers must inspect those diagnostics rather than treating an absent report
as a passing rebuild. This avoids cold source replay; it does not impose a
deadline on expensive changed geometry or cache misses.

## 24. Nonbinding native snapshots and batch save destinations (2026-09-12)

`doc_save(doc_id, path, preserve_path=false)` retains ordinary Save semantics:
the successful destination becomes the live path and clears dirty state.
With `preserve_path:true`, serialization runs in unregistered cached scratch.
Success or failure must leave the live path, tree, revision, dirty state and
undo history unchanged. Path-backed component references in the snapshot are
resolved against the live source destination before serialization; the live
relative references are not rewritten. The response includes the written
`path` and `preserve_path:true`. This is a nonbinding snapshot, not a general
Save As relative-path migration or a self-contained dependency bundle.

MCP autosave, legacy live publication, daemon autosave and orphan retirement
use nonbinding snapshots. Normal batch project saves retain an existing
non-recovery `.step+` destination even when a display name is supplied.
New, detached and recovered documents select the configured project directory.
Metadata failure must not guess a new destination; connection loss propagates.

## 25. Explicit interference classification versus exact clearance (2026-09-12)

Native `eval_interference` and registered `check_interference` accept
`clearance_mode:"exact"|"threshold"`, default `"exact"`. Existing exact-mode
distance work is unchanged, including explicitly requested legacy `quick`
approximations. Threshold mode shares motion's conservative classification:
valid AABB separation may prove a bound; otherwise exact Boolean Common
classifies overlap with the unchanged volume tolerance.

At zero minimum clearance with `include_coincident:false`, completed overlap
classification does not run an unnecessary exact curve-distance optimization.
Positive-clearance and coincident-contact requests still use exact distance
whenever bounds cannot decide the requested classification. Threshold mode
does not substitute legacy `quick` approximations for that required work.

A bound-only pair carries `distance_status:"lower_bound"` and
`distance_lower_bound_mm`, without `distance`. A pair actually measured in
threshold mode carries `distance_status:"exact"` and `distance`. Missing
distance is not zero. Legacy `approx:true` values remain explicitly bounds
in narration and desktop fit displays.

Member scopes, expected/checked pair counts and failure evidence are retained.
Failed geometry or pair work makes the report incomplete, never collision-free.
`ok` means complete with no classified volumetric interference; requested
minimum-clearance warnings remain separate. Threshold reports do not claim an
assembly-wide tightest measured gap. Neither mode guarantees cancellation or
a wall-clock bound inside OCCT validity, Boolean or exact-distance algorithms.

## 26. Implicit collision-only consumers (2026-09-12)

Registered `describe_assembly` and the component-axis steering sweep request
zero-clearance, no-coincident threshold classification instead of implicit
exact distances. Legacy `quick:true` is not an equivalent performance policy:
close pairs can still enter exact curve-distance optimization.

Assembly narration retains the native fit report and an explicit
`collision_free` qualification. Complete narration may report real overlaps;
zero checked pairs, missing accounting, geometry failures or pair failures
cannot qualify a document as collision-free. See `narration.md` for the
placement snapshot and live mate-solve ordering.

Steering samples retain pair accounting, failures and collision witnesses.
The aggregate reports `complete`, `frames_checked` and indexed failures;
requested incomplete checks make the sweep unsuccessful without discarding
valid sampled poses or known collisions. Connection loss aborts subsequent
motion mutations. `steering_turning_radius(check_interference=true)` exposes
the sampled trajectory so this evidence survives the registered tool boundary.

## 27. Exact selected-face fingerprints (2026-09-12)

This extends §22. Short SIDs are coarse lookup buckets, not globally unique
topology identities. A SID string or SID-only selector still requires a unique
face in the selected body. A complete `query_geometry` face selector instead
requires exactly one identical canonical fingerprint within its SID bucket;
owner and ordinal hints do not participate. Partial geometric selectors,
stale fingerprints, and genuinely identical ambiguous candidates are refused.

Several faces sharing a SID may retain independent finishes. Records remain
`face_appearances[bN][record_id] = {selector, appearance}`, but `record_id` is
opaque; the authoritative target is `selector`, not the dictionary key.
Rendering also requires a unique exact fingerprint, rather than SID uniqueness.
Clearing with a complete fingerprint clears only that target; clearing with a
SID clears all stored records carrying that SID, including stale records.
Assignments resolve against final batch geometry and apply in request order.

## 28. Native file metadata operations (2026-09-12)

`doc_file_operations` is an internal JSON dispatch operation for closed native
`.step+` files, not a document edit/compile/solve operation. The registered MCP
adapters are `inspect_native_files(paths, output_path?)` and
`relocate_native_files(plan_path)`. Every explicit adapter path is absolute and
inside `CHUDWARE_FS_ROOT`; the browser's raw engine bridge refuses this RPC.

Inspection accepts `{action:"inspect", paths:[...]}`. Relocation accepts
`{action:"relocate", entries:[{path,destination,name?,component_names?}],
path_map:{old_absolute:new_absolute}}`. Entries name distinct source and
destination files. Optional component names are keyed by stable component ID.
The complete plan is preflighted before writes: no overwrite, case-colliding
destination, malformed/disagreeing map, unsupported source schema, or unknown
component ID. Native processing is bounded one source at a time.

Copies change only document/component display names and authoritative file
references: component `source.path` and import `params.source_path`/`path`.
Mapped filesystem references always become relative to the destination parent,
including originally absolute references. Unmapped references are rebased to
retain their resolved target. The caller must establish required dependency
closure before treating the copied project as portable.

Embedded-BREP import paths are optional provenance; historical URI provenance
remains literal. Free-form document `meta` paths are documentary metadata,
reported with `required:false`, `rewrite:false`, and
`usage:"documentary_metadata"`. They must not be rewritten into a new claim of
verification or treated as unknown geometry inputs. Unsupported authoritative
reference forms remain explicit refusals.

Feature IDs/names, document/component IDs, other parameters, materials, finishes,
mates, reference frames, poses, embedded model data, `geometry.step` and unknown
archive payloads survive. Optional stale display/BREP caches are invalidated;
their engine identity is never forged. Native canonical and masked immutable
hashes are FNV64, explicitly not SHA256. Original source files are never removed.
File copies publish exclusively through the native atomic serializer.

Results are `{ok,action,files,failures}` with per-file `ok` and status. I/O races
may leave explicitly reported partial copies; this is not an all-files atomic
transaction. MCP persists large inspection results and every relocation result
in guarded, non-overwriting JSON artifacts. A failed artifact write preserves
native outcome counts and warns that copies may already exist.


## 29. `doc.studies_changed` and agent Motion studies (append-only, 2026-09-28)

`sim_study_add` / `sim_study_edit` / `sim_study_delete` stay read-only for access control
(a GUI viewer may edit the studies of an agent's doc), but each bumps the revision without
a tree, so hostd announces it on its own event — never as a `doc.changed` — to every
subscriber except the caller:

```json
{"event":"doc.studies_changed","data":{"doc_id":"…","revision":7,"source_client":3,
 "source_kind":"mcp","method":"sim_study_add"}}
```

A GUI re-pulls that document's `sim_study_list` on its next Simulation-tree draw. A
successful MCP `simulate_motion` records its inputs as the document's native Motion study
(`{type:"motion", name:"Motion: <mate names>", motion:{drivers, steps, loop, fps,
check_interference, min_clearance}}`, keyed by type+name, so re-sweeping the same joints
replaces it) and returns it as `motion_study {doc_id, study_id, name, view}`. Only inputs are
stored; the GUI re-solves them with the same engine `simulate_motion` call to play them.

## 30. `sketch_edit_entity` shape and component source base (append-only, 2026-10-10)

This section replaces the `sketch_edit_entity` row in the Sketch table.

`{op:"sketch_edit_entity", sketch, entity:{id, <fields>}}` is the canonical form. The patch is
`entity` without `id`. A null value deletes the key. `{sketch, id, params:{<fields>}}` stays
valid, because `sketch_drag_commit` sends it. The id comes from `id` or from `entity.id`.
The op refuses with a named `detail.reason` in these cases:

| reason | condition |
|---|---|
| `two_patches` | The op has both `entity` and `params`. |
| `patch_not_object` | The patch is not a JSON object. |
| `bad_entity_id` | The id is not a string. |
| `conflicting_entity_id` | `id` and `entity.id` are different. |
| `missing_entity_id` | The op gives no id. |
| `unknown_entity` | The sketch has no entity with that id. |
| `empty_patch` | The patch changes no field. |
| `type_change` | The patch changes `type`. A `type` equal to the current type is accepted. |
| `unknown_entity_field` | A key is not a stored key of the entity, not `construction`, and not an optional field of its type. |

An op never returns `ok:true` when it ignored its patch.

A relative component `source.path` resolves against the folder of the assembly file. An
unsaved assembly resolves against the absolute `project_dir` that `doc_new` received. The
process working directory is never a base. If no base exists, compile refuses the component
with `component_source_unresolved`. If the file does not exist, compile refuses with
`component_source_missing` and names the resolved path and the base. Save rebases paths as
before. If no base exists, save writes a relative path without change.

`sketch_add_constraint` with `type:"fix"` first writes the solved geometry of every entity
into its authored coordinates, if the sketch converges. Solved geometry does not change. Thus
a fix locks the shape that the readback shows. If the sketch does not converge, the authored
coordinates stay. A sketch that does not converge reports the conflicting constraint ids and,
for each coincident between fixed points, the authored gap.

## 31. Sketch references, Fully Define and composite relations (append-only, 2026-10-16)

**`sketch_convert`** accepts every reference kind of doc_schema "Sketch relation set v2 … external
reference kinds" (vertex, edge `mode:"pierce"`, datum axes/origin, `sketch_entity`). It resolves
the sketch plane and projects from the session's cached compile of the candidate document (the
op never runs its own uncached compile), and the associative refresh reuses one per-model-state
set of face/edge/vertex descriptors plus a per-ref projection memo keyed by source, resolved
sketch plane and compiled body identity, so an unchanged model re-projects nothing. Send all
picked refs of one action in ONE op (`refs:[...]`): one compile, one undo step. id_map is
unchanged: `$i` the first new entity, `$i#k` the rest in ref order; later ops of the same batch
may reference them (`sketch_add_constraint` refs `["e7", "$0"]`).

**`sketch_fully_define`** — `{op:"sketch_fully_define", sketch, relations?:true, dimensions?:true,
anchor?:"origin"|"none", entities?:[eid…]}`. Infers the relations a user would add (coincident,
horizontal/vertical, endpoint tangent, parallel, perpendicular, exact equal lengths/radii,
concentric, midpoint, collinear, point_on, origin anchoring), then driving dimensions for the DOF
left (diameter/radius, line length, h/v location from the anchor, angle to `axis.h`, h/v
location of every free spline through-point). Every value is the current measurement: the op
never moves geometry, and each candidate is accepted only if the sketch still converges with no
conflicting/redundant constraint and the DOF drops. `entities` restricts inference to those
entities. id_map: `$i` the first new constraint id, `$i#k` the rest; nothing when it added
nothing. It throws only for a bad sketch ref; callers read the DOF from `sketch_health`.

**Composite entities** expand with their intent: `rectangle` → 4 lines + 4 coincident +
2 horizontal + 2 vertical (rotated: 4 coincident + 2 parallel + 1 perpendicular); regular
`polygon` → closed coincident chain + equal sides; `polyline` → coincident chain (+ closure).

**MCP `add_features`** appends `sketch_fully_define` after each new sketch feature and after
`sketch_add_entity` runs in the SAME batch (one revision, unchanged rollback), unless the call
(`auto_define:false`), the op, or the sketch (`params.auto_define:false`) opts out. The readback
reports relations/dimensions added and the resulting DOF; `check_sketch` reports `pinned_only`
(DOF 0 reached through `fix` pins with no driving dimension) and never calls such a sketch fully
defined.

## 32. Soft sketch auto-definition v1.2 (append-only, 2026-10-26)

Additive amendment to §4/§31: constraints minted by `sketch_fully_define` carry `auto:true`
(doc_schema "Soft sketch auto-constraints v1.1"). Existing untagged constraints stay explicit.
Explicit constraints added through `sketch_add_constraint`, inline `sketch_add_entity`
constraints, or other sketch mutation ops supersede soft auto constraints. The engine solves
all explicit intent first, then re-admits prior auto constraints in their original order,
keeping their ids only while the solve converges without conflicts or redundancy. It removes
rejected auto constraints, permits geometry to move to satisfy the explicit intent, and
fully-defines any remaining freedom with new `auto:true` constraints. The op layer mints ids;
the caller's explicit constraint still maps to the ordinary `$i` id-map slot.

Explicit-vs-explicit conflicts retain normal over-definition reporting; the engine never
drops explicit intent to accommodate another constraint. `sketch_set_dim` value/expression
edits promote the edited constraint to explicit and settle other auto constraints by the same
rule. Placement-only edits do not promote; deletion remains ordinary deletion without refill.
`sketch_probe` applies these same rules to its read-only copy, so GUI preflight does not report
over-definition solely because the new intent supersedes an auto constraint.


## 33. Assembly diagnostic rank and MCP pose ownership (append-only)

Native mate residual DOF is the free variable count minus the accepted constraint
rank already used to classify mates. A second rank estimate with a different
scale-dependent cutoff must not add fictitious freedoms to an offset rigid lock
chain. Explicit angle-drive mobility remains additive in the existing mechanism
`dof` readback; `residual_dof` remains the prescribed-pose nullity. This diagnostic
change does not relax convergence tolerances, make unresolved mates successful,
or alter the numerical pose solve.

The MCP `add_features` pose guard evaluates ownership after the planned batch's
mate/component state transitions. Deleting or suppressing every live mate and
moving the freed component can be one atomic batch. A final active mate still owns
a floating component's pose, including a mate added or re-enabled by the batch.
Native invalid-id, dependency and rollback rules remain authoritative.

## 34. Native relocation direct-reference availability (append-only)

After a relocation batch finishes, the native result reports
`direct_reference_availability:{required_count,missing_count,unknown_count,scope}` for
required filesystem references inventoried in documents whose copies have status `written`
(including a copy written before a subsequent source-stamp failure). `scope` is
`direct_references_in_written_files`. Each filesystem `dependency_rewrites` row retains
`exists`, sampled at source inventory, and adds `destination_exists`, sampled after the
batch completes. If the destination check fails, `destination_exists` is null and
`destination_check_error` gives the error; it counts as unknown, not missing.
References are counted individually, not deduplicated by pathname. Optional embedded-import
provenance and documentary metadata do not contribute to required counts.

This is a point-in-time check of direct references, not a transitive dependency-closure,
geometry, or portability validation. A target published by another entry in this batch is
available; a mapping alone does not make a later-batch target available. Missing or unknown
required references produce `direct_reference_warning`, but do not change native copy
success, trigger implicit copying or deletion, or bypass preflight guards. If preflight
fails before a batch is processed, the availability summary is absent, not a closure claim.
The MCP bounded reply includes these counts, a warning when applicable, and up to five
missing-reference previews with source existence and destination; the full native result
is retained in the guarded artifact. Report-write failure preserves the native outcome
and these diagnostics in the reply.

## 35. Assembly source-refresh revisions and bulk sketch constraints (append-only)

Refreshing a referenced part commits a revision when already-published assembly geometry,
solved poses, or reference bindings change. The assembly becomes dirty, its pre-refresh
authored state enters undo, and redo is cleared. Initial hydration and unchanged repeated
reads do not increment the revision. Undo restores authored state, not external source
files; referenced geometry still follows the current source.

Activation, rebuilding, geometry/assembly queries, rendering, analysis and saving may
refresh sources. Guarded apply/solve operations refresh before checking the supplied
revision, so a guard from before a source change is stale even if no explicit assembly
edit occurred.

After document locks are released, hostd publishes the advancing snapshot to every
attached subscriber as `doc.changed`, with `source_client:-1`, `source_kind:"system"` and
`method:"assembly_source_refresh"`; it has no user or actor attribution. Equal-revision
cache verification retains the original writer's attribution.

Replacing a sketch's constraints through `edit_feature` applies the same soft
auto-constraint rules as individual constraint operations: an explicit dimensional
constraint may replace an automatically supplied dimension without treating that soft
dimension as a conflicting user requirement. Unrelated relations remain intact.

## 36. OpenSCAD source and managed conversion (append-only)

A `scad` feature holds `params.source` (the exact OpenSCAD source), `files`
(portable relative `path` and `data_b64` dependency pairs), `parameters` (typed
overrides), `parameter_schema` (entries with `name`, `kind`, optional `label`),
and `mm_per_source_unit` (finite positive, default 1). Kinds are `length`,
`number`, `integer`, `angle`, `boolean`, and `string`. Length overrides use
canonical mm, angles degrees. Simple literal discovery does not infer that every
number is a length or evaluate arbitrary source expressions. Complex code-owned
expressions remain in source. Source and controls edit the same feature.

`scad_status` reports availability of the external OpenSCAD executable and
confinement; `scad_inspect({params})` discovers controls without a render.
Actual rendering supports OpenSCAD syntax, including modules, include/use,
loops, booleans and extrusions, with only staged dependencies available. Rendering
is isolated and has no unsandboxed fallback. Resulting geometry is faceted, not
analytic native geometry or recovered arbitrary feature history.

The revision-guarded document operation
`{op:"scad_convert", direction:"to_code"|"to_native", mm_per_source_unit?:number}`
is also exposed as MCP `scad_convert`. `to_code` projects only supported native
graphs: box, cylinder, sphere, cone, explicit boolean and rigid transform, subject
to supported options and reported blockers. Managed metadata preserves the original
graph and parameter mapping. `to_native` only restores an intact managed projection;
changed source, dependencies, mapping or incompatible equations are refused rather
than restoring stale history. Parameter edits within the supported mapping can be
restored. Arbitrary imported SCAD has no native graph to recover. Conversion and
other edits use the normal atomic operation/compile transaction: failure leaves the
previous revision and model intact.

Native `.step+` save retains source, dependencies, controls and the successful build
cache. `.scad` export retains effective overrides and scale with portable sidecars;
browser export can deliver source and sidecars as a ZIP. See the format reference
for installation and platform confinement prerequisites.

## 37. Rollback-prefix execution and agent readback (2026-10-29, additive)

The native compiler enforces §4's rollback bar in geometry, tessellation, ownership
and compile caches. Suffix features remain in the document with
`rebuild.feature_status[].rolled_back:true` and `ok:true`: they were not compiled,
not successfully built. Clearing `after` restores the complete history. Implicit
`add_feature` insertion advances the bar; an explicit string `before` remains
authoritative. Deleting the bar feature moves it to the previous surviving feature
or clears it when none remains.

MCP `add_features` preserves `rolled_back` in each returned feature status.
`get_part(summary=True)` and selected-feature reads retain an active
`tree.rollback_after`, even when the selected records do not include the bar feature.
This feature-level flag is distinct from a failed batch's top-level `rolled_back`,
which reports whether the rejected mutation was reverted.

## 38. Document Ops v2 — native part compile transactions (append-only)

This is the **v2 semantic contract** for native part `doc_apply_ops`; the wire
protocol and document schema strings do not change. It supersedes §2.3 and the
statement in §5 that rebuild failures are not errors **for this operation**.
Those v1 statements remain above as historical text. Assembly operations and
explicit `doc_rebuild` have their own rebuild paths; this amendment does not
assert that they have the same rejection envelope.

The worker validates the batch and compiles its candidate part before committing.
If the candidate does not compile, the whole batch is rejected: the previous
document tree, revision, dirty state, displayed geometry and undo/redo history
remain in effect. This includes failures of a fillet or of a downstream feature.
Validation errors retain their structured op error envelope; a compile rejection
is instead returned in-band in the otherwise transport-successful response:

- `rejected_tree` is the candidate definition for diagnosis, **not** the live
  document; `rebuild.feature_status` describes that candidate and contains the
  failure. A client must check `rejected_tree`, not infer acceptance from
  transport success or replace its live statuses with candidate statuses.
- `tree` and `revision` are the restored live state, `id_map` is empty, and
  `owner_map`, `datums`, `volume` and, when requested, `tess` describe the
  restored geometry. `with_tess:false` still omits the response mesh.
- The `rolled_back` field mentioned in §37 is on an MCP `add_features` failure
  result and reports whether that batch was reverted. It is not a required
  top-level field of the raw worker response. It is distinct from each
  `feature_status[].rolled_back` rollback-bar flag.

On acceptance, any verified fillet correction of literal radius parameters and
the requested edit commit together under the existing batch/coalescing undo
rules; rejection commits neither. Expression-driven radius intent is retained,
with any necessary effective value kept as derived state rather than substituted
for the expression. The returned tree and structured `data.fillet` status are
described in [doc_schema.md](doc_schema.md) and
[worker_rpc.md](worker_rpc.md); callers must not derive the numeric correction
by parsing warning text.

## Persistence attribution amendment — v2.1

On hostd, an advancing GUI mutation is human work unless its request params carry
`"origin":"system"`. The tag identifies system-initiated work on a GUI connection; it
does not change an op's geometry or transaction semantics. Metadata-only name and meta
changes advance a separate `metadata_revision` and participate in human-dirty tracking.
The registry maintains saved and last-human revisions in both dimensions; a binding save
clears human dirty only when it covers the last human changes. `doc_reload` resets that
state. The document creator's `origin_kind` is not a substitute for this attribution.
See the hostd persistence amendment in [worker_rpc.md](worker_rpc.md) for wire fields,
save refusal and recovery semantics.
