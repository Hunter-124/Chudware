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
                         then driving dims). Draw roughly -> relations -> critical dimensions ->
                         sketch_fully_define -> check_sketch: dof 0 and not pinned_only.
  sketch_auto_constrain(id)  stitch near-touching endpoints coincident (apply=True, default) or
                         just propose them (apply=False); each add is rollback-safe (skips any that
                         would conflict) and it reports which chains closed. Runs automatically after
                         edits when doc meta sketch.auto_coincident is on (default).
  sketch_fully_define(id) infer residual editable relations/dimensions (never blanket fix).
                         relations=True, dimensions=True, anchor="origin"|"none", entities=[ids]
                         optionally limit it. add_features runs this in the same revision after
                         NEW sketches / sketch_add_entity batches; auto_define:false opts out.
  describe_scene(views)  the TEXT equivalent of render_part/contact_sheet for a model with no
                         vision: per requested view (default front/right/top/iso), which faces
                         face the camera (by sid/surf/side/dims) plus the part bbox — computed
                         from B-rep normals with NO occlusion modeling (says so inline). This is
                         the non-vision self-review step; do it instead of skipping the review.
  inspect_part(target)   CLOSE-UP of a face/edge/feature (plain-word query or feature id fN):
                         the ENGINE frames the target's own bbox, so a 5 mm hole in a 120 mm
                         plate is RENDERED at 5 mm scale, not cropped out of a whole-part shot.
                         view="auto" is the shot planner's single best MEASURED camera (every
                         candidate is measured in the engine); for an internal target that is
                         a SECTION through it, cut face hatched and the removed half ghosted.
  inspect_feature(target) planner close-ups (hero + complementary alternates, a section for
                         anything internal) + whole-part CONTEXT with the target highlighted,
                         plus describe_part excerpt + dims. Use when the grid is ambiguous.
  render_views(views?)   the planner's 7 complementary views (or a custom view list, e.g. the
                         6-ortho+iso drawing set); optional target= frames every tile on the
                         feature, and the caption names what each view shows.
  look_around(target)    N close-ups of one target tiled: the planner's complementary set by
                         default (ranked=False for an even orbit), each labelled with how much
                         of the target it shows.
  Every emphasis render paints the target's EXACT visible faces (shading kept so curvature
                         still reads), desaturates the rest, and reports measured coverage —
                         a camera that cannot see the target SAYS so instead of showing a blank.
  hover(sid)             expand ONE face/edge by the sid describe_part / query_geometry gave —
                         its full card (surf, side, signed normal, concavity bore/boss, radius).
  query_adjacency()      the face-adjacency graph: which faces touch, and whether each shared
                         edge is a CONVEX outside-corner or a CONCAVE inside/pocket edge. This is
                         how you reason about "the edge between the top and front face".

Every query_geometry/describe_part match now also carries: `side` (top/bottom/left/right/front/
back), `normal` (SIGNED outward dir — unlike the unsigned `axis`), `concavity` (bore vs boss),
`radius`, `axis_point` (cylinder/cone: a point ON the axis — `centroid` is off-axis on a fillet),
torus faces (sweep bends) `major_radius`/`minor_radius`/`torus_axis`/`torus_center` (bend
radius, tube radius, bend axis, bend centre — their `radius`/`axis` stay null), and a stable
`sid`. add_features returns a `changes` write-diff ("added 1 cylinder; 1
new bore; volume -3927; part bbox 60x40x20 mm") so you can confirm an op did what you meant
instead of guessing: `changes.bbox_mm` is the whole part's size (check it against what you were
asked for rather than spending a measure_part call), a volume move too small to be real material
says so instead of printing a delta, and anything the kernel objected to arrives as an
"ENGINE WARNING:" prefix on the summary. Best-effort `overlap_warnings` also travels when the
batch introduced a boss/cut overlap (see check_feature_overlap for the dedicated tool).
```

