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
