# Guide: Gsl

```text
GEOMETRY-SEMANTICS LAYER — read a part WITHOUT drowning in a flat face dump, and get a
usable read on geometry EVEN WITHOUT VISION (no image, just text).

On a complex/imported part, do NOT page through query_geometry's whole face list. Instead:
  describe_part()        a compact NAMED outline: bbox, the feature groups WITH a placement
                         narrative (which face each feature sits on / touches), any detected
                         symmetry/instanced PATTERN (e.g. "6-fold rotational about +Z" with the
                         6 blade faces collapsed to one line), the bores/holes and flat faces by
                         side, and a stable `sid` for every addressable face/edge. Token cost
                         scales with the number of DISTINCT roles, not the raw face count.
  describe_sketch(id)    a sketch's plane placement, entity roster (line/circle/arc + dims),
                         constraint roster with driving-dimension values, closed-region count/
                         area, and the DOF (fully/under/over-defined) verdict — the sketch
                         equivalent of describe_part, for reviewing layout before you extrude it.
  sketch_health(id)      the narrated constraint read: WHICH free variables remain (dof in plain
                         words), open chain endpoints, per-entity under/full/over, redundant/
                         conflicting with removal hints, and an ORDERED schedule (relations first,
                         then driving dims) to reach dof 0 — the sketch loop is edit -> sketch_health
                         -> add the scheduled relations / sketch_auto_constrain -> repeat.
  sketch_auto_constrain(id)  stitch near-touching endpoints coincident (apply=True, default) or
                         just propose them (apply=False); each add is rollback-safe (skips any that
                         would conflict) and it reports which chains closed. Runs automatically after
                         edits when doc meta sketch.auto_coincident is on (default).
  describe_scene(views)  the TEXT equivalent of render_part/contact_sheet for a model with no
                         vision: per requested view (default front/right/top/iso), which faces
                         face the camera (by sid/surf/side/dims) plus the part bbox — computed
                         from B-rep normals with NO occlusion modeling (says so inline). This is
                         the non-vision self-review step; do it instead of skipping the review.
  hover(sid)             expand ONE face/edge by the sid describe_part / query_geometry gave —
                         its full card (surf, side, signed normal, concavity bore/boss, radius).
  query_adjacency()      the face-adjacency graph: which faces touch, and whether each shared
                         edge is a CONVEX outside-corner or a CONCAVE inside/pocket edge. This is
                         how you reason about "the edge between the top and front face".

Every query_geometry/describe_part match now also carries: `side` (top/bottom/left/right/front/
back), `normal` (SIGNED outward dir — unlike the unsigned `axis`), `concavity` (bore vs boss),
`radius`, and a stable `sid`. add_features returns a `changes` write-diff ("added 1 cylinder; 1
new bore; volume -3927") so you can confirm an op did what you meant instead of guessing — and,
best-effort, `overlap_warnings` when the batch introduced a boss/cut overlap (see
check_feature_overlap for the dedicated tool).
```

