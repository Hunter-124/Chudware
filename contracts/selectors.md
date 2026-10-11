# Contract: Selectors — stable references into B-rep topology

> How a document references "that face" across rebuilds. **Geometric resolution, not persistent
> naming** — this is documented honestly: topology-splitting edits can break references, which
> then surface as dangling-ref warnings (never a silent wrong pick).

## 1. Selector object

```json
{"kind": "face", "surf": "cylinder", "centroid": [12.0, 0.0, 3.0],
 "axis": [0.0, 0.0, 1.0], "area_or_len": 94.2,
 "owner_feature": "f4", "hint_index": 2,
 "normal": [1.0, 0.0, 0.0], "side": "right", "concavity": "bore", "radius": 3.0}
```

**Scored fingerprint** (used by `resolve()` §3) — these alone determine a match:

| field | meaning |
|---|---|
| `kind` | `face` \| `edge` \| `vertex` \| `body` \| `datum` \| `region` |
| `surf` | faces: `plane|cylinder|cone|sphere|torus|bspline|other`; edges: `line|circle|ellipse|bspline|other`; else `null` |
| `centroid` | `[x,y,z]` mm (vertices: the point; regions: `[u,v]` sketch coords). Rounded to 1e-6 (noise-free). |
| `axis` | unit vector: cylinder/cone axis, plane normal, line direction, circle axis; else `null`. **Unsigned** (opposite faces collide — use `normal` to tell them apart). |
| `area_or_len` | face area mm², edge length mm, region area mm²; vertices/bodies `null` |
| `owner_feature` | feature id whose compilation created this topology (compiler topology-diff tag); pattern/mirror copies inherit the pattern feature's id |
| `hint_index` | tiebreak ordinal among same-owner same-kind topology at creation time |

**Informational fields** (GSL Stage 0) — emitted so the LLM read-back is self-describing;
`resolve()` IGNORES them, so they never change a match. Present on query/read-back selectors;
`null` where not applicable.

| field | meaning |
|---|---|
| `normal` | **signed** outward direction at the centroid, for EVERY face incl. curved (plane normal / cyl-cone radial-out / curved-cap outward). Disambiguates the two opposite faces that share an unsigned `axis`. `null` for edges/vertices/degenerate. |
| `side` | plain-word label of `normal` (Z-up: `top`/`bottom`/`right`/`left`/`front`/`back`); `null` for an oblique face with no dominant axis. The same vocabulary used to address geometry on input. |
| `concavity` | round face only: `bore` (concave — a hole/void) vs `boss` (convex — a peg/fillet); `null` otherwise. Set on the query read-back; host-pick selectors leave it `null` (kernel helper is read-back-local). |
| `radius` | radius (mm) of a cylinder/cone/sphere face or a circle edge; `null` otherwise. |
| `axis_point` | cylinder/cone face only: the face centroid projected onto the surface axis — a point ON the axis (`centroid` is off-axis on a partial cylinder such as a fillet). `null` otherwise. |

Special forms:
- **datum**: `{"kind":"datum", "feature":"f2"}` — or a bare reserved id string (`plane.top`, …)
  wherever the schema says "selector-or-datum".
- **region** (sketch profile loop): `{"kind":"region", "sketch":"f3", "centroid":[u,v],
  "area_or_len": a, "hint_index": loop_ordinal}` — loops ordered by (cx, cy) for stability.
- **body**: `{"kind":"body", "owner_feature":"f7", "hint_index": n}` (multi-body booleans/mirror).

## 2. Creation

On `doc_resolve_pick`, the worker builds the selector from the picked topo entity:
surface type via `BRepAdaptor_Surface(face).GetType()`, centroid/area via `BRepGProp`,
axis from the adaptor (cylinder/cone/plane), `owner_feature` from the compiler's topology tags,
`hint_index` from creation-time ordinal among siblings.

## 3. Resolution (each rebuild) — normative algorithm

For selector S over the rebuilt body's topology map:

1. **Candidates** = same-`kind` entities tagged with `owner_feature == S.owner_feature`;
   if that set is empty, all same-kind entities.
2. **Score** each candidate c (lower is better):
   - `dc = |centroid(c) − S.centroid| / bbox_diag(document)`
   - `ax = 1 − |dot(axis(c), S.axis)|` (only when both axes exist)
   - `ar = |area(c) − S.area_or_len| / max(area(c), S.area_or_len)` (when both exist)
   - `tm = 0 if surf-type matches else 1`
   - With axis: `score = 0.4·dc + 0.3·ax + 0.2·ar + 0.1·tm`
   - Without axis: `score = 0.55·dc + 0.3·ar + 0.15·tm`
3. **Accept** the best candidate iff `best < 0.35` AND `(second_best − best) > 0.05`
   (ties broken by nearest `hint_index`). Otherwise the reference is **dangling**.
4. Dangling refs: the owning feature compiles in error state with
   `dangling_refs: ["<refslot>[i]"]`, tree shows the warning badge + "What's wrong" text;
   the feature is skipped (downstream builds on last good body). The PM's selector well
   highlights red for re-pick.

## 4. Regions

Resolved within the owning sketch after solve: re-detect loops, score by centroid distance
(sketch-space, normalized by sketch bbox diag) + area term (same weights, no axis/type terms).
Same accept thresholds.

## 5. Test obligations (selector torture suite, M1)

- Edit an upstream dimension that MOVES a filleted edge → fillet re-resolves correctly.
- Delete the owner feature of a referenced face → dangling, feature errors, tree badge.
- Pattern a cut, then edit seed → pattern children still resolve (inherited owner).
- Two symmetric candidate faces → margin rule forces dangling rather than guessing
  (and `hint_index` rescue when owners match).

## 6. Torus read-back (appended)

Torus faces (sweep bends, revolved fillets) carry four additional informational fields on
query/read-back selectors and host-pick selectors. They are emitted ONLY on torus faces
(absent, not `null`, elsewhere); `resolve()` ignores them; `axis`/`radius` stay `null` on a
torus, so the scored fingerprint, the `parallel`/`perpendicular` filters and the stable `sid`
are unchanged. Like `axis_point`, they are excluded from persisted `face_appearances`
fingerprints and are tolerated (ignored) when a match is handed back as a query.

| field | meaning |
|---|---|
| `major_radius` | torus major radius (mm): axis to the tube centre circle — the bend's centreline radius |
| `minor_radius` | torus minor radius (mm): the tube radius of that face (outer wall r_o, inner wall r_i) |
| `torus_axis` | unit direction of the torus axis (the bend's rotation axis); sign as the kernel stores it |
| `torus_center` | the torus origin: the bend centre, a point ON the torus axis |

## 7. near: geometric distance (appended, 2026-10-09)

A geometry query's `"near":[x,y,z]` (`core/doc/picks.cpp` `rank_near`, the ONE implementation:
`doc_query`/`query_geometry`, feature refs, measure refs, mate/fixture faces and the engine's
own centroid re-resolution all call `resolve_geom_query`) ranks the candidates left after
`surf`/`parallel`/`perpendicular`/`side`/`pick`/`body_id` by

    key = min( distance(point, trimmed sub-shape geometry), distance(point, centroid) )

- The geometry distance is exact (BRepExtrema point→face/edge, honouring trims), so a point ON
  the entity scores 0 even when its centroid is far away (the outer rim circle of a flange,
  whose centroid is the axis point). Ranking by centroid alone picked a PCD bolt-hole edge.
- The centroid term keeps the documented "hand a match back" workflow: a centroid read from
  `describe_part`/`query_geometry` (or a resolved selector's `centroid`, which the engine
  re-resolves this way) also scores 0, even for curved entities whose centroid is off-surface.
- Ties: candidates whose key is within `max(1e-4 mm, 1e-6 × key)` of the best go to the one
  with the nearer centroid, then to `TopExp::MapShapes` order. A point on the shared edge of
  two faces therefore picks the face whose centroid is nearer; concentric circles/cylinders
  sharing a centroid stay ambiguous — narrow them with `pick:"inner"|"outer"` or `surf`.
- `count` (default 1) keeps that many, repeating the rule over the remaining candidates;
  `index` then indexes this order.

Changing this rule changes which entity a saved query resolves to, so it is a geometry-cache
epoch (`contracts/stepplus.md` §12).

## 8. Profile region picks (appended, 2026-10-10)

This section extends §4. `resolve_profile_faces` implements it.

- A `centroid` pick resolves to the region with the nearest (u,v) centroid. `hint_index` is a
  seed. A closer region still wins.
- `centroid` alone is a valid region selector; `hint_index` is optional. If `hint_index` is
  present, it must be a number.
- The pick fails the feature if the centroid is outside the bounding box of that region. The
  box is grown by max(0.5 mm, 25% of its larger side).
- The pick fails the feature if two picks at different centroids resolve to the same region.
- Without `hint_index`, the pick fails the feature if two regions at different centroids are
  at the same distance.
- The error names `profile[i]` and the current region count.
- A pick never moves silently to a different region.

Before this rule, a pick snapped to the nearest surviving region and duplicates were dropped.
A sketch edit that removed regions then removed cuts with `ok:true`.
