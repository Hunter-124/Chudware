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
    "doc_status": {"active_sketch": null, "mates": null}
  },
  "tess": {"aicm_path": "…\\doc_6f0c_r18.aicm",
           "bodies": [{"body_id": 1, "name": "Body1", "color": [0.71,0.76,0.82]}]}
}
```

- `doc_status.active_sketch`: `"under" | "full" | "over" | null`; `doc_status.mates`:
  `"ok" | "over" | null` (assemblies).
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
