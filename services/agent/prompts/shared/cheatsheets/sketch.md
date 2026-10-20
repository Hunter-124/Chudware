SKETCH — draw roughly → geometric relations → driving dimensions → sketch_fully_define → check_sketch.
Coordinates are 2D mm on refs.plane: "plane.top" (XY), "plane.front" (XZ), "plane.right" (YZ),
or a measured face selector. Geometry fields are initial guesses, NOT constraints.
Read each mutation's sketch_readbacks; require a closed material profile, dof == 0,
pinned_only == false, no conflicts or skipped constraints before its solid feature.
Never blanket-fix authored geometry. Fixed associative external references are legitimate.

AUTHORING
- add_features accepts an ops array. add_feature uses feature:{type:"sketch",refs:{plane},params:{entities, constraints}}.
- Assign entity ids e1,e2,… and unique constraint ids c1,c2,… within each sketch.
  Handles: line e1.p1/e1.p2; circle/arc e2.center; arc e2.start/e2.end;
  spline e3.start/e3.end or e3.p0/e3.p1/…; point e4.p. Bare entity ids name curves.
  origin is a valid fixed point; axis.h/axis.v are fixed horizontal/vertical lines through it.
- circle {center:[u,v],radius}; line {p1:[u,v],p2:[u,v]};
  arc {center:[u,v],radius,start:<degrees>,end:<degrees>,ccw:true}; point {p:[u,v]}.
  rectangle {center:[u,v],width,height} expands into joined horizontal/vertical lines;
  polygon {center:[u,v],sides:6,across_flats:12} expands into equal joined sides.
  Other polygon sizes: exactly one of across_flats/circumradius/inradius; rotation is degrees.
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
  fix                  ["e9"]                               deliberate reference pin, NOT a definition strategy
  distance             ["origin","e2.center"]               value: center location (axis:"h"/"v" optional)
  distance             ["e3.p","e1"]                        value: point-to-line distance
  distance             ["e1","e2"]                          value: parallel-line separation
  length               ["e1"]                               value: line/slot spine length (distance also accepts one)
  angle                ["e1","axis.h"]                      value: degrees; lines/slot spines/virtual axes
  radius/diameter      ["e2"]                               value: circle/arc size, or slot cap radius/width
  arc_length           ["e2"]                               value: arc radius × absolute sweep (mm)

After explicit intent, append {"op":"sketch_fully_define","sketch":"f1","relations":true,"dimensions":true,"anchor":"origin"}.
Optional entities:["e1",…] limits the scope; anchor:"none" avoids origin anchoring.
add_features defaults auto_define:true for newly authored sketches and sketch_add_entity batches,
appending residual definition after explicit relations/dimensions in the same transaction.
For intentional staged/free geometry opt out with add_features(...,auto_define=false), or
"auto_define":false on the sketch feature, its params, or the authoring op. Then explicitly
fully-define and check before consuming it. Automatic definition does not replace functional dims.

EXTERNAL DESIGN REFERENCES — inspect selectors first; sketch_convert is associative by default.
Use construction:true to keep projected geometry OUT of the profile, then relate authored geometry
with coincident/point_on/concentric/distance to the returned entities. fix:true pins the reference,
not your profile. Each conversion returns $<op-index>, $<op-index>#1,… in ref order.
  {"op":"sketch_convert","sketch":"f1","construction":true,"fix":true,"associative":true,"refs":[{"kind":"edge","selector":{"sid":"<measured-edge-sid>"}}]}
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

EDIT OPS — existing sketch f1 and entity ids from readback (these are independent examples):
  {"op":"sketch_fillet","sketch":"f1","e1":"e1","e2":"e2","radius":2}
  {"op":"sketch_trim","sketch":"f1","entity":"e3","point":[10,4],"keep_construction":true}
  {"op":"sketch_mirror_entities","sketch":"f1","entities":["e1","e2"],"line":"e9"}
  {"op":"sketch_pattern_entities","sketch":"f1","entities":["e1"],"mode":"linear","count":3,"dir":[1,0],"spacing":12}
  {"op":"sketch_pattern_entities","sketch":"f1","entities":["e1"],"mode":"circular","count":4,"center":[0,0],"angle":360,"equal_spacing":true}
Fillet trims two lines and adds a true arc, coincidents and tangents. Trim removes the span
under point (optionally retains it as construction). Mirror requires a real line entity e9,
not a virtual axis; it adds symmetry relations. Pattern count INCLUDES the seed; copies still
need design intent/definition. Re-read after topology edits, then fully-define and check.

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
