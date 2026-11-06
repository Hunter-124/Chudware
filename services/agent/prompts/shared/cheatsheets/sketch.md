SKETCH — draw roughly → geometric relations → critical dimensions only → auto-definition → check_sketch.
Relations first; never pin with `fix`; do not dimension what a relation implies; use construction
geometry and symmetry. Coordinates are 2D mm on refs.plane: "plane.top" (XY), "plane.front" (XZ),
"plane.right" (YZ), or a measured face selector. Geometry fields are initial guesses, NOT constraints.
Read each mutation's sketch_readbacks; require a closed material profile when the consumer needs a
region. Rib and sweep paths can intentionally remain open; revolve can auto-close to its axis.
Check dof == 0, pinned_only == false, no conflicts or skipped constraints.
Never blanket-fix authored geometry or delete a partially dependent relation wholesale.

AUTHORING
- add_features accepts an ops array. add_feature uses feature:{type:"sketch",refs:{plane},params:{entities, constraints}}.
  Author geometry + relations + critical dims in the SAME call: auto-definition runs after
  the batch's explicit intent. Prefer symmetric about axes/centerlines, equal, midpoint and
  point_on axis over measured coordinates. Dimension functional sizes and partner spacings,
  not every edge; use expressions/globals via eq_set for values shared across features.
- Assign entity ids e1,e2,… and unique constraint ids c1,c2,… within each sketch.
  Handles: line e1.p1/e1.p2; circle/arc e2.center; arc e2.start/e2.end;
  spline e3.start/e3.end or e3.p0/e3.p1/…; point e4.p. Bare entity ids name curves.
  origin is a valid fixed point; axis.h/axis.v are fixed horizontal/vertical lines through it.
- circle {center:[u,v],radius}; line {p1:[u,v],p2:[u,v]};
  arc {center:[u,v],radius,start:<degrees>,end:<degrees>,ccw:true}; point {p:[u,v]}.
  rectangle {center:[u,v],width,height} expands into joined horizontal/vertical lines;
  polygon {center:[u,v],sides:6,across_flats:12} expands into equal joined sides; give exactly ONE of
  across_flats|circumradius|inradius|diameter (n>=5 is regular: construction circle + equal sides); rotation in degrees.
  spline {points:[[u,v],…]} is for genuinely freeform geometry; airfoil {naca:"2412",chord:120,aoa:2} for wings.
- Use true circular arcs and tangent lines for slots/rounded profiles, not faceted polylines
  or spline substitutes. A straight slot is two parallel lines plus two semicircular arcs
  (Example C); native slot/arc_slot solver entities are not a substitute for an extrudable wire.
- Symmetric profiles: one centerline (construction:true) or axis.v, one half-size/spacing,
  symmetric relations, then only the independent dimensions. Do not dimension both halves.

RELATIONS / DIMENSIONS — put these constraint objects in params.constraints, or append with
  {"op":"sketch_add_constraint","sketch":"f1","constraint":{"id":"c20","type":"radius","refs":["e2"],"value":{"value":4,"expr":null}}}
Create a dimension this way; sketch_set_dim only RETUNES an existing dimension:
  {"op":"sketch_set_dim","sketch":"f1","id":"c20","value":5}
Dimensions use value:{value:<number>,expr:null}; a real global/equation may replace expr.
The following are alternative constraint examples, NOT one combined constraint set:

  type                 refs example                          meaning / supported geometry
  coincident           ["e1.p2","e2.start"]                 join points (also ["e3.center","origin"])
  horizontal/vertical  ["e1"] or ["e1.p1","e2.p1"]          line orientation / align two points
  parallel             ["e1","axis.h"]                      lines or slot spines
  perpendicular        ["e1","axis.v"]                      lines or slot spines
  collinear            ["e1","e2"]                          same infinite line; axes allowed
  point_on             ["e3.p","e1"]                        point on line/circle/arc/ellipse/conic/spline
  intersection         ["e3.p","e1","e2"]                   point on both curves (lines infinite)
  midpoint             ["e3.p","e1"]                        point at line midpoint or arc mid-sweep
  concentric           ["e2","e3.center"]                   circle/arc/ellipse/ellipse_arc/arc_slot centers or points
  coradial             ["e2","e4"]                          circles/arcs: same center and radius
  equal                ["e1","e2"]                          same family: lines, circles/arcs, ellipses,
                                                             slots (length+width), arc_slots (radius+width)
  equal_curve_length   ["e1","e2"]                          line/arc pairs, including mixed pairs
  tangent              ["e1","e2"]                          supported curves, not line-line; ellipse allowed
  tangent              ["e3.end","e2"]                      spline endpoint G1 (first/last .pK also valid)
  smooth               ["e3.end","e2"]                      spline end G2 to line/arc/circle/spline end;
                                                             add coincidence separately
  symmetric            ["e1.p1","e2.p2","axis.v"]           points about a line or axis.h/axis.v;
                                                             also two whole lines or two circles/arcs
  fix                  ["e9"]                               pins; external references only, never your own profile
  distance             ["origin","e2.center"]               value: center location (axis:"h"/"v" optional)
  distance             ["e3.p","e1"]                        value: point-to-line distance
  distance             ["e1","e2"]                          value: parallel-line separation
  signed_distance      ["e1.p1","e2.p1"]                   signed displacement along axis:"h"|"v"; value:{value,expr}
  equal_offset         ["e1.p1","e2.p1","e3.p1","e4.p1"]  equal signed point-pair offsets along axis:"h"|"v"
  length               ["e1"]                               value: line/slot spine length (distance also accepts one)
  angle                ["e1","axis.h"]                      value: degrees; lines/slot spines/virtual axes
  rotate_point         ["e1.p1","e2.p1","e3.p"]             signed degrees value:{value,expr}; target = pivot + R(value) × (source − pivot)
  radius/diameter      ["e2"]                               value: circle/arc size, or slot cap radius/width
  arc_length           ["e2"]                               value: arc radius × absolute sweep (mm)

rotate_point followers may use angle_ref:"cN" to mirror an independent explicit driving
rotate_point master in the same sketch. Edit the master with sketch_set_dim; follower
value/expr/driven edits are rejected. Deleting the master cascades followers and reports their
ids. Overview exposes angle_ref; sketch_health.angle_links lists the links.

AUTO-DEFINITION — sketch_fully_define (relations:true, dimensions:true, anchor:"origin"|"none", entities:[…])
runs by default (auto_define:true) after your explicit intent in the same batch; opt out with
add_features(auto_define=false) or "auto_define":false on the feature/params/op, then define and check
before consuming. It adds relations first, then dimensions, all tagged auto:true; unknown ids are an error.
Measured values are ROUNDED: a driving value v snaps to the nearest multiple of S when
|snapped - v| <= min(0.1*S, 0.005*|v|); S = 1 mm if |v|>=100, 0.5 mm if 10<=|v|<100, 0.1 mm if 1<=|v|<10,
0.05 mm if |v|<1; angles use S = 1 deg. Other values are kept as measured, cleaned to 1e-4
(39.98 -> 40; 29.93 and 12.7 unchanged). round:false keeps exact measured values.
The add_features reply carries sketch_definitions[] {notes, dof_before, dof_after, rounded}; notes
explain anything refused or left free. Read authored_dimensions vs auto_dimensions (findings
all_dimensions_auto, dimension_heavy): auto fills the rest, it never replaces critical dimensions.
sketch_health `hints` (formerly `schedule`) are suggestions, not a plan. Conflict rows list
conflicts_with partner ids ("c3 (distance 40) conflicts with c1 (distance 30), c2 (horizontal)").
check_sketch / sketch_health include revision only if stable across the read; revision_stale:true
means it changed or was unavailable. Re-check before relying on the verdict.

PROBE — sketch_probe(sketch, add_constraint | set_dim | delete_constraint) dry-runs ONE change on a
copy (never touches the open sketch): ok, dof_before/after/delta, converged, conflicting, redundant,
skipped, conflicts_with, would_over_define. Probe before adding a driving dimension.

FACE SKETCHES — refs.plane = a measured planar face selector places the sketch on that face: origin
= the face's area centroid (centre of mass of the face surface, not a bbox centre), u = world +X
projected onto the face (+Y when the face normal is ±X), v = normal × u, normal = outward face
normal. Datum planes are fixed at the world origin: top u=+X v=+Y n=+Z; front u=+X v=+Z n=-Y;
right u=+Y v=+Z n=+X. Face-local coordinates are relative to the centroid, so use sketch_convert
of the face/edges (or coordinates measured from query_geometry minus the centroid) to hit features.

EXTERNAL DESIGN REFERENCES — inspect selectors first. sketch_convert defaults construction:true,
fix:true, associative:true: projected geometry is REFERENCE, not profile (pass construction:false
to use it as profile); fix:true pins the converted refs (legitimate for external geometry only,
never for your own profile). Relate authored geometry to the returned entities with
coincident/point_on/concentric/distance. Each conversion returns $<op-index>, $<op-index>#1,… in ref order.
  {"op":"sketch_convert","sketch":"f1","refs":[{"kind":"edge","selector":{"sid":"<measured-edge-sid>"}}]}
Ref alternatives (real selectors/ids from readback, never invented):
  {"kind":"face","selector":{"sid":"<face-sid>"}}          all boundary wires
  {"kind":"vertex","selector":{"sid":"<vertex-sid>"}}      projected point
  {"kind":"edge","selector":{"sid":"<edge-sid>"},"mode":"pierce","near":[0,0]}
  {"datum":"axis.x"} / {"datum":"axis.y"} / {"datum":"axis.z"} / {"datum":"origin"}
  {"datum":"front"} / {"datum":"top"} / {"datum":"right"}  plane intersection
  {"kind":"sketch_entity","sketch":"f2","entity":"e1"}
A pierce ref must cross the sketch plane; near selects among crossings. Projected axes may
collapse to a point when perpendicular. After conversion, a following constraint can use
refs:["e1.p1","$<convert-index>.p"] for a converted vertex/pierce point.

EDIT OPS — existing sketch f1, entity ids from readback (op, required keys; all take "sketch"):
  op                      keys                                  constraints registered
  sketch_fillet           e1,e2,radius{value,expr}              arc + coincident + tangent + driving radius (conflict rejects batch)
    Radii clamp to the valid corner range (op_reports: requested_radius, applied_radius).
    Out-of-range expressions are preserved inside min(max((expr),1e-6),upper); op_reports
    gives expression_clamp_min/max and bounds_basis. Bounds snapshot the corner at operation
    time: later source-geometry changes can invalidate the frozen upper bound.
  sketch_chamfer          e1,e2 (lines),distance{value,expr}    driving chamfer distance (+ optional distance2)
  sketch_offset           entities,distance,side:[u,v]          offset-distance dim + parallel (lines) / concentric (arcs, circles)
  sketch_pattern_entities entities,mode:linear|circular,count   linear: spacing dims + parallel/equal; circular: angular driving dims/relations
                          +dir,spacing | center,angle           (count INCLUDES the seed)
  sketch_mirror_entities  entities,line (real line, not axis)   symmetric relations
  sketch_move / rotate    entities,dx,dy | entities,cx,cy,angle  driving dims carrying value/expr; copy:true duplicates
    Move shares signed_distance driving components via equal_offset relations across copies;
    expressions support zero values and sign crossings.
    In-place move/rotate snapshots anchor pose (circle center, line start/direction, arc
    center/start/end directions), leaving individual length/radius dimensions editable.
    Multi-entity joint edits, line/arc orientation changes or arc sweep changes can still conflict.
  sketch_trim             entity,point,keep_construction?       removes the span under point
  sketch_extend           entity,end:"start"|"end"              extends to the next curve
  sketch_paste_entities   entities_bundle                       none — re-relate the pasted geometry
  sketch_delete_entity / sketch_delete_constraint  id           unknown id is an error; op_reports[] lists dropped_constraints
Offset supports line/arc chains; offset a circle separately. Circular pattern defaults equal_spacing:true
at ±360°, dividing the angle by count; explicit false rejects that full turn. Otherwise equal_spacing:false
uses angle as the step, true divides angle by count. Each step must be nonzero; patterns
stacking a copy on the seed are rejected, including circular patterns of a circle centered on
the pivot. Rotations support centered circles. Circular patterns and rotations use signed
rotate_point relations for copied point handles, preserving driving expressions.
Polygon n>=5 is regular: construction circle + equal sides + vertices on the circle.
Fillet/chamfer/offset/move/rotate/pattern numeric dimensions accept {value,expr}, e.g.
spacing:{"value":12,"expr":"pitch/2"}; expressions remain driving, including computed spacing/angle
arithmetic for copies. Re-read after topology edits; op_reports[] carries each op's notes.

WORKED SOLIDS — complete ops arrays; in interactive work check_sketch before the solid op.
A sketch alone has no volume. extrude refs.profile is the sketch; mode boss fuses, cut removes,
new creates an independent body. Revolve uses refs.axis; loft uses refs.sections; sweep uses refs.path.

Example A — origin-centered r8 post, driven by radius (not fixed coordinates):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"PostProfile","refs":{"plane":"plane.top"},"params":{"entities":[{"id":"e1","type":"circle","center":[0,0],"radius":8}],"constraints":[{"id":"c1","type":"coincident","refs":["e1.center","origin"]},{"id":"c2","type":"radius","refs":["e1"],"value":{"value":8,"expr":null}}]}}},
 {"op":"sketch_fully_define","sketch":"$0"},
 {"op":"add_feature","feature":{"type":"extrude","name":"Post","params":{"mode":"new","depth":10},"refs":{"profile":"$0"}}}
]

Example B — two equal holes symmetric about axis.v, with one center spacing and one radius:
[
 {"op":"add_feature","feature":{"type":"box","name":"Plate","params":{"length":60,"width":40,"height":6}}},
 {"op":"add_feature","feature":{"type":"sketch","name":"HoleCenters","refs":{"plane":"plane.top"},"params":{"entities":[{"id":"e1","type":"circle","center":[-20,0],"radius":3},{"id":"e2","type":"circle","center":[20,0],"radius":3}],"constraints":[{"id":"c1","type":"symmetric","refs":["e1","e2","axis.v"]},{"id":"c2","type":"point_on","refs":["e1.center","axis.h"]},{"id":"c3","type":"distance","refs":["e1.center","e2.center"],"value":{"value":40,"expr":null}},{"id":"c4","type":"radius","refs":["e1"],"value":{"value":3,"expr":null}}]}}},
 {"op":"sketch_fully_define","sketch":"$1"},
 {"op":"add_feature","feature":{"type":"extrude","name":"Holes","params":{"mode":"cut","end":"through_all"},"refs":{"profile":"$1"}}}
]

Example C — real 28×8 slot profile: 20mm center spacing plus r4 semicircles, tangent joins:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"SlotProfile","refs":{"plane":"plane.top"},"params":{"entities":[{"id":"e1","type":"line","p1":[-10,4],"p2":[10,4]},{"id":"e2","type":"arc","center":[10,0],"radius":4,"start":270,"end":90,"ccw":true},{"id":"e3","type":"line","p1":[10,-4],"p2":[-10,-4]},{"id":"e4","type":"arc","center":[-10,0],"radius":4,"start":90,"end":270,"ccw":true}],"constraints":[{"id":"c1","type":"coincident","refs":["e1.p2","e2.end"]},{"id":"c2","type":"coincident","refs":["e2.start","e3.p1"]},{"id":"c3","type":"coincident","refs":["e3.p2","e4.end"]},{"id":"c4","type":"coincident","refs":["e4.start","e1.p1"]},{"id":"c5","type":"tangent","refs":["e1","e2"]},{"id":"c6","type":"tangent","refs":["e2","e3"]},{"id":"c7","type":"tangent","refs":["e3","e4"]},{"id":"c8","type":"tangent","refs":["e4","e1"]},{"id":"c9","type":"horizontal","refs":["e1"]},{"id":"c10","type":"symmetric","refs":["e2.center","e4.center","axis.v"]},{"id":"c11","type":"point_on","refs":["e2.center","axis.h"]},{"id":"c12","type":"radius","refs":["e2"],"value":{"value":4,"expr":null}},{"id":"c13","type":"distance","refs":["e2.center","e4.center"],"value":{"value":20,"expr":null}}]}}},
 {"op":"sketch_fully_define","sketch":"$0"},
 {"op":"add_feature","feature":{"type":"extrude","name":"SlotBody","params":{"mode":"new","depth":5},"refs":{"profile":"$0"}}}
]
