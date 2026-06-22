DOC OPS CHEATSHEET. You emit a JSON array of document ops. Units mm / degrees.
Dimension params are objects: {"value": <num>, "expr": null}. A later op refers to a feature
created earlier IN THE SAME ARRAY as "$<index>" (0-based op position); refer to existing tree
features by their id (e.g. "f1").

PICK THE RIGHT FEATURE — model like a person would, with standard editable features. Do NOT
approximate a curved or organic body with a stack of boxes.
  - Round/curved body that is the same all the way around an axis (bottle, vase, fuselage, nozzle,
    knob, wheel)            -> SKETCH the half-profile + REVOLVE it about an axis.
  - Shape that blends between different cross-sections (boat hull, wing, blended duct, turbine
    blade, transition)      -> SKETCH each cross-section on its own plane + LOFT through them.
  - Constant cross-section that follows a path (pipe, handle, gasket, extruded rail, spring/thread)
                            -> SKETCH the profile + a path + SWEEP (helix path for a thread/spring).
  - Slab / boss / pocket with a flat 2D outline (bracket, plate, rib, gusset) -> SKETCH + EXTRUDE.
  - Repeated holes/bosses (bolt circle, vent grid)  -> make ONE, then LINEAR/CIRCULAR PATTERN it.
  - Symmetric feature                                -> make one side, then MIRROR it.
  - Aerofoil / wing section                          -> AIRFOIL sketch entity (see cheatsheets/airfoil).
  - A plain rectangular/cylindrical block            -> a primitive (box/cylinder) is fine.
Use a raw `script` feature ONLY when no combination of the features below can express the geometry.

----------------------------------------------------------------- ADD geometry (op "add_feature")

PRIMITIVES — box/cylinder/sphere/cone/torus. Centered on the origin by default; an optional
`at:[x,y,z]` MOVES the primitive (and `centered:false` puts its base/corner at `at`). `mode`
"boss"|"cut" and `merge` combine it with the running body (a cylinder mode:"cut" bores a hole).
  box {length,width,height}   cylinder {radius,height}   sphere {radius}
  cone {r1,r2,height}         torus {major_r,minor_r}     +optional {at:[x,y,z], centered:false}

SKETCH + a 2D-profile feature — this is how you make anything that is not a bare primitive.
  1) add_feature type "sketch", refs.plane = "plane.top" | "plane.front" | "plane.right"
     (NEVER "XY"/"XZ"/"YZ"). plane.top = XY (+Z up), plane.front = XZ, plane.right = YZ.
  2) sketch_add_entity ops (refer to the sketch as "$<its op index>"). Entity types:
        circle {center:[x,y], radius}      line {p1:[x,y], p2:[x,y]}
        arc {center:[x,y], radius, start:<deg>, end:<deg>, ccw:<bool>}      point {p:[x,y]}
        spline {points:[[x,y],[x,y],...]}  — a smooth curve THROUGH the points (use for organic /
                aerofoil / cam outlines). Close a region by making the first and last point meet
                another edge. A rectangle = 4 lines (there is no "rect" entity).
        airfoil {naca:"2412" OR dat:"<path>", chord:<mm>, aoa:<deg>, points:50, closedTE:true}
                — expands to the upper+lower aerofoil splines as one closed loop (see airfoil sheet).
  3) Turn the sketch into a solid with ONE of:
     EXTRUDE  params {mode:"boss"|"cut", end:"blind"|"through_all", depth:<mm>},
              refs.profile = [{"kind":"region","sketch":"$<idx>"|"f<id>","centroid":[x,y],"hint_index":0}]
              (extrudes/cuts ALL the sketch's regions).
     REVOLVE  params {mode, angle:{value:360,...}}, refs.profile = [region],
              refs.axis = an axis datum {"kind":"datum","feature":"axis.x"|"axis.y"|"axis.z"}
              (or a sketch line edge). The profile must lie OUTSIDE / beside the axis (offset it).
     LOFT     params {mode}, refs.sections = [region, region, ...] (>= 2 region selectors, each
              naming a DIFFERENT sketch on its own plane, in order). Optional params.ruled:true.
     SWEEP    params {mode}, refs.profile = [region] (the cross-section),
              refs.path = a selector naming the PATH sketch (an open line/arc/spline, or a `helix`).

DRESS / TRANSFORM an existing body — name edges/faces with a geometry QUERY (no clicking needed):
  fillet  {radius}  refs.edges = {"of":"edge", ...}      chamfer {length} refs.edges = {...}
  shell   {thickness} refs.remove = {"of":"face","side":"+z"}   (omit remove = closed shell)
  draft   {angle}  refs.neutral = a planar face/datum (the parting plane), refs.faces = the faces
          to taper {"of":"face", ...}
  Query predicates: {"of":"edge"|"face"} all; add "side":"+z"/"-z"/.. , "parallel":[x,y,z],
  "surf":"circle"/"plane"/.. , "near":[x,y,z]+"count". Use the doc_query tool first if unsure.

REPEAT / SYMMETRY. The seed feature(s) in refs.features MUST be earlier sketch-based features
that carry geometry to copy — an EXTRUDE boss/cut, a HOLE, a REVOLVE/SWEEP/LOFT. A bare primitive
(box/cylinder) is NOT patternable; make the repeated thing with sketch+extrude, then pattern that.
  linear_pattern  params {count1:<int>, spacing1:<dim>, count2?:<int>, spacing2?:<dim>},
          refs.dir1 = {"kind":"datum","feature":"axis.x"}, refs.dir2? = a datum axis, refs.features = ["f3", ...].
  circular_pattern params {count:<int>, angle:<dim, total, dflt 360>, equal_spacing?:<bool>},
          refs.axis = {"kind":"datum","feature":"axis.z"}, refs.features = ["f2", ...].
  mirror  refs.plane = {"kind":"datum","feature":"plane.right"} (or a planar face), refs.features = ["f2", ...].
  `count*` are plain INTEGERS; spacings/angles are dim objects.
  Reserved datums: planes "plane.top"/"plane.front"/"plane.right", axes "axis.x"/"axis.y"/"axis.z",
  point "origin" — referenced as {"kind":"datum","feature":"<name>"}.

----------------------------------------------------------------- EDIT an existing feature
  edit_feature   {"op":"edit_feature","id":"f1","params":{"length":{"value":80,"expr":null}}}
  set_suppressed {"op":"set_suppressed","id":"f3","value":true}
  delete_feature {"op":"delete_feature","id":"f3","cascade":true}
  rename         {"op":"rename","id":"f1","name":"BasePlate"}

----------------------------------------------------------------- EXAMPLES

A — 40x40x20 block, then round ALL its edges 3mm:
[
 {"op":"add_feature","feature":{"type":"box","name":"Block","params":{"length":{"value":40,"expr":null},"width":{"value":40,"expr":null},"height":{"value":20,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"fillet","name":"Round","params":{"radius":{"value":3,"expr":null}},"refs":{"edges":{"of":"edge"}}}}
]

B — a 60x40 plate 6mm thick with four 3mm corner holes (sketch circles + extrude-cut):
[
 {"op":"add_feature","feature":{"type":"box","name":"Plate","params":{"length":{"value":60,"expr":null},"width":{"value":40,"expr":null},"height":{"value":6,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"sketch","name":"Holes","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[24,14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[-24,14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[24,-14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[-24,-14],"radius":3}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Drill","params":{"mode":"cut","end":"through_all"},"refs":{"profile":[{"kind":"region","sketch":"$1","centroid":[0,0],"hint_index":0}]}}}
]

C — a vase as a SOLID OF REVOLUTION. The half-profile must be a CLOSED region: the curved outer
wall (a spline) plus a straight line back down the Z axis to close it. Revolve 360 about Z:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Profile","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.front"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"spline","points":[[0,0],[15,0],[18,20],[10,45],[12,60],[0,60]]}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[0,60],"p2":[0,0]}},
 {"op":"add_feature","feature":{"type":"revolve","name":"Body","params":{"mode":"boss","angle":{"value":360,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[8,30],"hint_index":0}],"axis":{"kind":"datum","feature":"axis.z"}}}}
]

D — a blended duct by LOFTING a circle into a square (two sketches on parallel planes):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Bottom","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[0,0],"radius":20}},
 {"op":"add_feature","feature":{"type":"sketch","name":"Top","params":{"entities":[],"constraints":[],"offset":{"value":50,"expr":null}},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$2","entity":{"type":"line","p1":[-15,-15],"p2":[15,-15]}},
 {"op":"sketch_add_entity","sketch":"$2","entity":{"type":"line","p1":[15,-15],"p2":[15,15]}},
 {"op":"sketch_add_entity","sketch":"$2","entity":{"type":"line","p1":[15,15],"p2":[-15,15]}},
 {"op":"sketch_add_entity","sketch":"$2","entity":{"type":"line","p1":[-15,15],"p2":[-15,-15]}},
 {"op":"add_feature","feature":{"type":"loft","name":"Blend","params":{"mode":"boss"},"refs":{"sections":[{"kind":"region","sketch":"$0","centroid":[0,0],"hint_index":0},{"kind":"region","sketch":"$2","centroid":[0,0],"hint_index":0}]}}}
]

E — 6 bosses evenly around the Z axis: make ONE as a sketch+extrude boss (a primitive can't be
patterned), then circular-pattern that extrude feature about axis.z:
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Base","params":{"radius":{"value":40,"expr":null},"height":{"value":5,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"sketch","name":"BossSk","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[30,0],"radius":4}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Boss","params":{"mode":"boss","end":"blind","depth":{"value":10,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$1","centroid":[30,0],"hint_index":0}]}}},
 {"op":"add_feature","feature":{"type":"circular_pattern","name":"Ring","params":{"count":6,"angle":{"value":360,"expr":null},"equal_spacing":true},"refs":{"axis":{"kind":"datum","feature":"axis.z"},"features":["$3"]}}}
]
