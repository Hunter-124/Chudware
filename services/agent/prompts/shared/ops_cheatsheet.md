DOC OPS CHEATSHEET. You emit a JSON array of document ops. Units mm / degrees.
Dimension params are objects: {"value": <num>, "expr": null}. A later op refers to a feature
created earlier IN THE SAME ARRAY as "$<index>" (0-based op position); refer to existing tree
features by their id (e.g. "f1").

NATIVE FORMAT — Chudware's native, editable file is .step+. It stores the WHOLE parametric feature
tree plus its simulation studies (and any 2D drawings) in one container the app reopens fully
editable. Save deliverables as .step+ so the user keeps an editable part; a plain STEP/STL is a
flattened export (geometry only — no feature history, no studies).

PICK THE RIGHT FEATURE — model like a person would, with standard editable features. Do NOT
approximate a curved or organic body with a stack of boxes.
  - Round/curved body that is the same all the way around an axis (bottle, vase, fuselage, nozzle,
    knob, wheel)            -> SKETCH the half-profile + REVOLVE it about an axis.
  - Shape that blends between different cross-sections (boat hull, wing, blended duct, turbine
    blade, transition)      -> SKETCH each cross-section on its own plane + LOFT through them.
  - Constant cross-section that follows a path (pipe, handle, gasket, extruded rail, spring/thread)
                            -> SKETCH the profile + a path + SWEEP (for a spring/coil/thread: a
                            HELIX feature as the path — no sketch needed for the coil path).
  - Thin support web bridging two walls (gusset, rib, stiffener)
                            -> open sketch contour (just the web outline) + RIB.
  - Taper a wall for moulding/casting (draft angle, tapered extrusion wall)
                            -> build the base shape first, then add a DRAFT feature on the wall faces.
  - Combine or subtract two separately-built bodies
                            -> BOOLEAN (fuse/cut/common) referencing the two feature ids.
  - Slab / boss / pocket with a flat 2D outline (bracket, plate, rib, gusset) -> SKETCH + EXTRUDE.
  - Repeated holes/bosses (bolt circle, vent grid)  -> make ONE, then LINEAR/CIRCULAR PATTERN it.
  - Symmetric feature                                -> make one side, then MIRROR it.
  - Folded constant-thickness metal (bracket, enclosure, chassis, tray) -> SHEET METAL: sm_base_flange
    then sm_edge_flange / sm_hem / sm_jog bends (see cheatsheets/sheetmetal).
  - Welded frame from stock tube/angle/channel/beam (chassis, rack, railing) -> WELDMENTS: sketch the
    member path, then weld_member with a profile (see cheatsheets/weldment).
  - Freeform / blended thin-wall shell or cover a solid can't make cleanly -> SURFACING: loft/sweep a
    surface, then surf_thicken it into a wall (see cheatsheets/surfacing).
  - Aerofoil / wing section                          -> AIRFOIL sketch entity (see cheatsheets/airfoil).
  - A plain rectangular/cylindrical block            -> a primitive (box/cylinder) is fine.
Use a raw `script` feature ONLY when no combination of the features below can express the geometry.

READ THE RESULT — add_feature's response carries a `changes` summary ("added 2 faces
(+2 cylinder); 2 new bore/hole(s); volume 24000→23435; part bbox 60x40x20 mm"), or
"no geometry change", or "volume effectively UNCHANGED ... too small to be real material",
or an "ENGINE WARNING:" prefix naming exactly what the kernel objected to. `changes.bbox_mm`
is the whole part's overall size — check it against the size you were asked for instead of
spending a measure_part call. Read all of this before building the next feature on the
mutation instead of assuming the op did what you meant.

----------------------------------------------------------------- ADD geometry (op "add_feature")

PRIMITIVES — box/cylinder/sphere/cone/torus. Centered on the origin by default; an optional
`at:[x,y,z]` MOVES the primitive (and `centered:false` puts its base/corner at `at`). `mode`
"boss"|"cut" and `merge` combine it with the running body (a cylinder mode:"cut" bores a hole).
  box {length,width,height}   cylinder {radius,height}   sphere {radius}
  cone {r1,r2,height}         torus {major_r,minor_r}     +optional {at:[x,y,z], centered:false}
  RELATIONAL `at` — instead of coordinates, `at` can be a face/edge QUERY: the primitive
  centres on that geometry. `at:{"of":"face","side":"top"}` = on the top face. Add
  `at_offset:[dx,dy,dz]` to nudge in world axes (e.g. a boss raised 10mm: at_offset:[0,0,10]).

SKETCH + a 2D-profile feature — this is how you make anything that is not a bare primitive.
  1) add_feature type "sketch", refs.plane = "plane.top" | "plane.front" | "plane.right"
     (canonical names). plane.top = XY (+Z up), plane.front = XZ, plane.right = YZ.
     SKETCH ON AN EXISTING FACE: set refs.plane to a face QUERY — e.g. {"of":"face","side":
     "top"} ("sketch on the top face"). Your 2D origin (0,0) is then the CENTRE of that face,
     so draw relative to it — no need to know its world height. Optional `offset`:<mm> lifts
     the sketch plane along the face/datum normal (a sketch N mm above a face).
     ⚠ For a BOSS on top of an existing part, sketch on the FACE ({"of":"face","side":"top"}),
     NOT "plane.top": primitives are CENTRED on the origin, so plane.top cuts through the
     MIDDLE of a box — a boss sketched there extrudes INTO the part and adds nothing.
  2) sketch_add_entity ops (refer to the sketch as "$<its op index>"). Entity types:
        circle {center:[x,y], radius}      line {p1:[x,y], p2:[x,y]}
        arc {center:[x,y], radius, start:<deg>, end:<deg>, ccw:<bool>}      point {p:[x,y]}
        rectangle {center:[x,y], width, height}  — PREFERRED way to make a rectangle (1 op,
                expands to 4 closed lines). Also {corner1:[x1,y1], corner2:[x2,y2]}. width/height
                accept bare numbers or {"value":N,"expr":null}. (4 separate lines still work.)
        spline {points:[[x,y],[x,y],...]}  — a smooth curve THROUGH the points (use for organic /
                aerofoil / cam outlines). Close a region by making the first and last point meet
                another edge.
        airfoil {naca:"2412" OR dat:"<path>", chord:<mm>, aoa:<deg>, points:50, closedTE:true}
                — expands to the upper+lower aerofoil splines as one closed loop (see airfoil sheet).
  3) Draw roughly → relations → functional driving dimensions → sketch_fully_define.
     origin, axis.h and axis.v are valid refs. Example circle constraints:
     {"id":"c1","type":"coincident","refs":["e1.center","origin"]},
     {"id":"c2","type":"radius","refs":["e1"],"value":{"value":8,"expr":null}}.
     Use unique eN/cN ids. Symmetric profiles use a construction centerline or axis.v;
     dimension only independent half-sizes/spacing. Use real tangent arcs/lines for slots,
     not faceted polylines/splines. Require check_sketch: closed, dof == 0, not pinned_only,
     no conflicts/skipped constraints. Never blanket-fix authored profiles.
     add_features defaults auto_define:true after explicit intent. Staged work opts out
     with call auto_define:false or auto_define:false on feature/params/authoring op.
     Then explicitly define before use; initial entity coordinates are not constraints.
  4) Turn the checked sketch into a solid with ONE of:
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

DRESS / TRANSFORM an existing body — name edges/faces with a geometry QUERY (no clicking
needed), same vocabulary for faces AND edges. INSPECT before you ACT: when you cannot name the
geometry with certainty, call query_geometry FIRST and build the pick from what it returns
rather than guessing a filter. You never need a face number, an edge number, or a coordinate.
  fillet  params {radius} refs.edges = {"of":"edge", ...}
          VARIABLE-RADIUS: add params.radius2 — a linear taper r→r2 along each edge (requires
          explicit edge picks, not a direction filter). e.g. r=3, radius2=1 tapers each selected
          edge from 3mm to 1mm.
  chamfer params {length} refs.edges = {...}
          ASYMMETRIC: add params.length2 — leg d1=length on one incident face, d2=length2 on the
          other (requires explicit edge picks). e.g. length=2, length2=0.5.
  shell   {thickness} refs.remove = {"of":"face","side":"top"}   (omit remove = closed shell)
  draft   params {angle (deg)}, refs.neutral = a planar datum OR a face query (the parting/neutral
          plane — the hinge about which faces tilt), refs.faces = the faces to taper {"of":"face",...}
          Optional refs.pull = an axis datum (pull direction; defaults to neutral plane normal).
          Positive angle = narrows in the pull direction (draft inward / SolidWorks default).
          e.g. 3° draft on all side walls of a block, neutral = bottom face:
          {"type":"draft","name":"Draft","params":{"angle":{"value":3,"expr":null}},
           "refs":{"neutral":{"of":"face","side":"bottom"},"faces":{"of":"face","surf":"flat"}}}
  Query predicates (combine freely; omit all = EVERY edge/face of `of`):
    "side": which side it's on — "top"/"bottom"/"left"/"right"/"front"/"back" (top=+Z, front=-Y;
            signed axes "+z"/"-x"/.. also work). Keeps the faces/edges furthest that way.
            EXCEPTION with a size pick: side+pick "largest"/"smallest" means "the largest/
            smallest face FACING that way" — side filters to that facing, pick chooses by area
            (so {"side":"top","pick":"largest"} finds a base's big top even when a taller
            sliver face sits higher).
    "surf": the shape — faces "flat"/"round" (round matches ANY curved face: cylinder/cone/sphere/
            torus); also "cylindrical"/"conical"/"spherical"/"toroidal" for specific curved types.
            Edges: "straight"/"round" (or exact: plane/cylinder/cone/sphere/torus, line/circle).
    "parallel": which way it runs — "z" (="vertical"), "x", "y", or [x,y,z]. parallel:"z" = the
            vertical corner edges of a block.
    "pick": the size — faces "largest"/"smallest", edges "longest"/"shortest"; or
            "inner"/"outer" to tell coaxial round geometry apart (tube inner vs outer
            wall, bore vs rim) — e.g. {"of":"face","surf":"round","pick":"outer"}.
    "near":[x,y,z] + "count": the N edges/faces nearest a point.  "index":[i,..]: pick by ordinal.
  On CURVED bodies: a REVOLVE/LOFT/SWEEP body is mostly ONE freeform (B-spline) face, so
    surf:"flat" matches it 0 times — that is correct, there are no planar faces. A `side` query on
    such a body returns the freeform wall (the only face facing that way), not a flat cap, because
    there is no cap. If you need selectable FLAT end faces (e.g. the rim of a tube), build the body
    profile. Query predicates match zero or more than one? Narrow with near/count/index rather
    than guessing which one you meant. Call query_geometry / doc_query FIRST whenever you are
    not certain what edges/faces a part has — it returns each edge/face's shape (surf) + side so
    you can see what will resolve before you commit a fillet/chamfer/shell/draft to it.

HELIX / COIL / SPRING PATH (type "helix"). A helix is a CURVE feature — it emits no body by
itself; use it as refs.path of a SWEEP to make a spring, coil, or thread.
  Required: refs.base = a datum plane ("plane.top" etc., the helix base), params.pitch (mm per
  turn), params.height (total axial height). Optional params: radius (default 10), taper (deg,
  narrows toward top; 0 = straight), turns (overrides height = turns*pitch), start_angle (deg),
  shape:"spiral" (flat Archimedean spiral — set turns, not height; start_radius optional).
  VARIABLE PITCH: instead of pitch+height, give pitch_table — an array of rows
  {turn:<float>, pitch:<mm>, dia?:<mm>} (or bare [turn, pitch, dia?] arrays) — the radius
  and pitch interpolate piecewise-linear between rows. Needs >= 2 rows.
  e.g. simple 3-turn compression spring: profile circle r=1.5 on plane.front at (8,0),
  helix base=plane.top radius=8 pitch=4 height=12, sweep profile over helix.
  Full example (spring coil, profile + helix + sweep):
  {"type":"helix","name":"SpringPath","params":{"pitch":{"value":4,"expr":null},
   "height":{"value":12,"expr":null},"radius":{"value":8,"expr":null}},
   "refs":{"base":"plane.top"}}

RIB — thin support web grown from an open sketch contour (type "rib"). The sketch must contain
  an OPEN line/arc/spline that bridges the body. The engine drops the contour inward toward the
  body centroid, thickens it by `thickness`, trims it to the body bbox, and fuses it.
  refs.profile = [region selector pointing at the open contour sketch]. Optional params.thickness
  (default 3mm). The sketch plane is typically a side face or datum plane; the rib web grows
  perpendicular to that plane.
  e.g. a 2mm web sketch on plane.right, bridging a bracket:
  {"type":"rib","name":"Gusset","params":{"thickness":{"value":2,"expr":null}},
   "refs":{"profile":[{"kind":"region","sketch":"$2","centroid":[0,10],"hint_index":0}]}}

SPLIT — cut a body into pieces along a planar tool (type "split"). Each piece becomes a
  separate body. Optional params.keep: "both" (default), "above", or "below" (relative to the
  tool plane normal). refs.tool = a datum plane or a face selector.
  e.g. split a body at the midplane, keep both halves:
  {"type":"split","name":"Halves","params":{},"refs":{"tool":"plane.right"}}
  e.g. keep only the top half:
  {"type":"split","name":"Top","params":{"keep":"above"},"refs":{"tool":"plane.top"}}

BOOLEAN — explicit fuse/cut/intersect of two named bodies (type "boolean"). Use when you have
  built two separate bodies and want to combine them without relying on the implicit
  mode:"boss"/"cut" merging in extrude/revolve/sweep. params.op: "fuse" (union),
  "cut" (subtract the tool from the target), "common" (intersection). Also accepts
  "combine"/"subtract"/"intersect" as op aliases.
  Canonical form: refs.targets = [{"kind":"body","owner_feature":"f1"}],
                  refs.tools   = [{"kind":"body","owner_feature":"f3"}].
  Shorthand: params.a = "f1" (target body feature id), params.b = "f3" (tool body feature id)
  — the engine accepts either form. Both bodies are consumed; a single new body replaces them.
  A fuse must produce ONE connected solid; a cut/common must also yield ONE solid piece
  (disjoint results fail — position the bodies so they actually overlap before cutting).
  e.g. subtract body f3 from body f1 (canonical):
  {"type":"boolean","name":"CutResult","params":{"op":"cut"},
   "refs":{"targets":[{"kind":"body","owner_feature":"f1"}],
           "tools":[{"kind":"body","owner_feature":"f3"}]}}
  e.g. union two bodies (shorthand):
  {"type":"boolean","name":"Union","params":{"op":"fuse","a":"f1","b":"f2"}}

INTENT FEATURES — one op that is a whole recipe. PREFER these over hand-built sketch+cut+pattern
for the common machining features; they take a face in plain words and need no positions math.
  hole  — drill into a planar face. params {style:"simple"|"cbore"|"csink"|"tap", end:"through_all"
          |"blind", diameter, depth?(blind), cbore_d?/cbore_depth?, csink_d?/csink_angle?}.
          Size the bore by `diameter` (mm) OR by fastener standard `"standard":"M3"` (the engine
          resolves the clearance bore — M3->3.4mm; tap-drill when style:"tap"). For an "M3 clearance
          bore" just give {"standard":"M3"}.
          refs.face = a face query e.g. {"of":"face","side":"top"}. Optional `positions`:[[u,v],..]
          for several holes; omit = one at the centre.
          POSITIONS FRAME (deterministic): origin = the centroid of the face's OUTER boundary
          (holes already cut in the face do NOT shift it); u = world +X projected into the face
          (world +Y if the face is perpendicular to X); v = n×u, right-handed around the outward
          normal. So on a top face u=+X,v=+Y; on a front (−Y) face u=+X,v=+Z; on a right (+X)
          face u=+Y,v=+Z. bolt_circle uses the same frame (start_angle measured from u).
          WORLD placement instead: params `world_points`:[[x,y,z],..] (mm, world frame) — each
          names a point on the bore AXIS; the engine projects it along the face normal onto
          the face it enters (with a word query like side:"top", whichever face facing that way
          the axis hits first — e.g. each of four boss tops; reported) and refuses one whose
          axis enters no such face. Never both positions and world_points.
  bolt_circle — a RING of holes in one feature (no per-hole trig). params {diameter, count, pcd
          (bolt-circle diameter), style?, end?, depth?(blind), start_angle?:<deg>,
          world_center?:[x,y,z]}. refs.face = a face query (defaults to the top face); the ring
          is centred on the face centre, or on world_center (a point on the ring's axis). e.g. 6×⌀5 on a 40mm circle:
          {"type":"bolt_circle","params":{"diameter":5,"count":6,"pcd":40},"refs":{"face":{"of":"face","side":"top"}}}
  rib    — a thin web from an open sketch profile. refs.profile = [region]. (See sheet if present.)
  standard_part — a catalog HARDWARE part expanded into normal editable tree features (same
          generators as parts_make). PREFER over hand-modeling stock hardware. params {family
          (required), size|thread ("M3", "1/4-20", bearing "608"), length_mm (screws/standoffs/
          rod/dowel/insert; nuts/washers/bearings ignore it)}. Families: shcs, bhcs, fhcs,
          hex_bolt, hex_nut, washer_flat, washer_lock, standoff_round, standoff_hex,
          threaded_rod, dowel_pin, heat_set_insert, bearing. Invalid family → error lists keys.
          {"type":"standard_part","name":"Standoff","params":{"family":"standoff_hex","thread":"M3","length_mm":22}}

ADVANCED FEATURE FAMILIES — three families the engine fully supports (watertight geometry). Each has
its own detailed cheatsheet; the essentials to invoke them:
  SHEET METAL (sm_*) — a folded constant-thickness part. sm_base_flange {thickness}, refs.profile =
    [region], makes the flat base AND marks the part as sheet metal; then bend walls off its straight
    top-rim edges with sm_edge_flange {length,angle} / sm_hem {hem_type} / sm_jog {offset,angle}, each
    refs.edge = ONE straight edge on a thickness face — query_geometry({"of":"edge","surf":"line",
    "near":[x,y,z],"count":1}) FIRST and pass the RESOLVED match back (this slot rejects a bare query);
    sm_flat_pattern unfolds the developed blank. Detail: cheatsheets/sheetmetal.
  WELDMENTS (weld_*) — a frame of stock members. Sketch the centerline PATH, then weld_member
    {profile, +per-profile dims, joint:"miter"|"butt"|"cope"} -> ONE member body per path segment.
    profiles: square_tube/rect_tube/round_tube/angle_L/flat_bar/c_channel/i_beam/t_section. ! refs.sketch
    is a BARE STRING sketch id ("$0"), NOT a region selector. Detail: cheatsheets/weldment.
  SURFACING (surf_*) — zero-thickness surfaces you thicken into solids. Build a surface (surf_planar/
    surf_extrude/surf_revolve/surf_sweep/surf_loft/surf_ruled/surf_fill), then surf_thicken {thickness},
    refs.surface = {"kind":"body","owner_feature":"$N"}, for a constant wall (or surf_knit panels
    watertight). surf_revolve axis = {"kind":"datum","feature":"axis.z"}. Detail: cheatsheets/surfacing.

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
  point "point.origin" (canonical; bare "origin" also accepted) — referenced as {"kind":"datum","feature":"point.origin"}.
  The world origin is selectable in the FeatureManager and renders as a glyph in the viewport.
  WHAT'S PATTERNABLE/MIRRORABLE: sketch-based features (extrude/revolve/sweep/loft boss or cut,
  hole) and transforms. Primitives (box/cylinder/...) are NOT — rebuild them as sketch+extrude.

MOVE / PLACE A BODY (op "add_feature", type "transform"). Translate and/or rotate ANY body — a
lofted fuselage, an extruded wing — not just a primitive's `at`. Use it to position separate
components into one coordinate frame for assembly (e.g. move a tail to meet the fuselage).
  params: translate:[dx,dy,dz] (mm) and/or angle:<dim deg> with rotate:[ax,ay,az] (rotation axis),
          pivot:[x,y,z] (point the rotation turns about, default origin), and copy:true to KEEP the
          original and add a MOVED DUPLICATE as a new body (instancing; default false = move in place).
  top-level: "body":"f3" targets THAT feature's body (default = the current running body). The
          named body is honored exactly — so in a multi-body assembly you can transform any named
          component (not just the last one), and several per-body transforms in one batch each land
          on their own body.
  Rotation is applied first (about pivot), then translation. e.g. move a wing +120 in X and up 8:
  {"type":"transform","name":"PlaceWing","params":{"translate":[120,0,8]},"body":"f4"}
  e.g. yaw a body 30° about Z through the origin:
  {"type":"transform","name":"Yaw","params":{"angle":{"value":30,"expr":null},"rotate":[0,0,1]}}
  e.g. two instances of a body 50 apart: a transform with params {"translate":[50,0,0],"copy":true}
  ASSEMBLY: import_part / make_standard_part each add a body whose BASE seats at z=0, axis +Z
          (make_standard_part also returns its bbox + datum). Position each with its own
          transform body:"fN" — e.g. import a screw (f2), washer (f3), nut (f4), then transform
          each by id to its seat. Build hollow CURVED bodies with a wall-profile revolve/loft, not
          shell (shell is prismatic-only).

PLANE / OFFSET CONVENTIONS (Z-up). plane.top = XY (normal +Z), plane.front = XZ (normal +Y),
plane.right = YZ (normal +X). A sketch `offset`:<mm> shifts the sketch plane ALONG that normal
(positive = +Z on top, +Y on front, +X on right); a negative offset goes the other way. When
sketching ON a face, `offset` lifts along that face's outward normal.

----------------------------------------------------------------- SKETCH OPS (append to an existing sketch feature)

sketch_add_entity   {"op":"sketch_add_entity","sketch":"$0","entity":{...}}
                    Add a line/circle/arc/spline/polygon/rectangle/airfoil/point entity to a sketch.

                    REGULAR POLYGON (hexagons, nuts, bolt-heads, prisms) — prefer this
                    closed composite; it expands to equal joined sides. Functional size
                    and location still need dimensions/relations or residual definition:
                      {"op":"sketch_add_entity","sketch":"$0",
                       "entity":{"type":"hexagon","center":[0,0],"across_flats":16}}
                    - type: "polygon"/"ngon" (needs "sides":N) OR a named n-gon that implies its
                      count: triangle, pentagon, hexagon, heptagon, octagon, nonagon, decagon,
                      dodecagon (an explicit "sides" still overrides).
                    - size — give exactly ONE: "across_flats" (wrench/spanner size, flat-to-flat =
                      the usual way hex stock/nuts are specified), "circumradius" (centre→vertex),
                      or "inradius"/"apothem" (centre→edge). "rotation":<deg> optional (default: a
                      vertex on +X; use rotation:90 to put a flat on top).
                    e.g. a hex prism 16mm across flats, 20mm tall:
[
 {"op":"add_feature","feature":{"type":"sketch","refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"hexagon","center":[0,0],"across_flats":16}},
 {"op":"sketch_fully_define","sketch":"$0"},
 {"op":"add_feature","feature":{"type":"extrude","params":{"mode":"new","depth":20},"refs":{"profile":"$0"}}}
]

                    RECTANGLE — PREFERRED over 4 hand-drawn lines (1 op, always closes):
                      {"op":"sketch_add_entity","sketch":"$0",
                       "entity":{"type":"rectangle","center":[0,0],"width":80,"height":50}}
                    - center-form: center:[x,y] (default [0,0]), width, height (both > 0). Dims
                      may be bare numbers or {"value":N,"expr":null}. Optional rotation:<deg>.
                    - two-corner form: corner1:[x1,y1], corner2:[x2,y2] (order-independent).
                    - alias type "rect" also accepted. Expands to 4 CCW line segments.
                    Separate line entities are valid; define sizes/location, not fix-every-edge.

sketch_add_constraint {"op":"sketch_add_constraint","sketch":"f1","constraint":
                      {"id":"c20","type":"distance","refs":["e1","e2"],"value":{"value":8,"expr":null}}}
                    Two lines: parallel-line separation. Other dimension types: length,
                    radius, diameter, angle, arc_length; distance also accepts point-point,
                    point-line and one line (length). Driving value = {value,expr}.
                    Relations: coincident, horizontal/vertical, parallel/perpendicular,
                    collinear, tangent, equal, concentric, coradial, midpoint, point_on,
                    intersection, smooth, symmetric, equal_curve_length; fix only for
                    deliberate reference pins. See cad_guide('sketch') for typed ref examples.
sketch_set_dim     {"op":"sketch_set_dim","sketch":"f1","id":"c20","value":10}
                    RETUNES an existing dimension; create one via sketch_add_constraint.
sketch_fully_define {"op":"sketch_fully_define","sketch":"f1","relations":true,"dimensions":true,"anchor":"origin"}
                    Residual definition AFTER explicit intent. Optional entities:[eid,...],
                    anchor:"none". Check DOF and pinned_only afterwards; not a blanket fix.

sketch_convert      {"op":"sketch_convert","sketch":"f1","refs":[{"kind":"edge","selector":{"sid":"<measured-sid>"}}],
                     "construction":true,"fix":true,"associative":true}
                    Project model references as construction, then relate the profile to them.
                    Ref alternatives: {kind:"face"|"edge"|"vertex",selector:{sid:...}},
                    {kind:"edge",selector:{sid:...},mode:"pierce",near:[u,v]},
                    {datum:"front"|"top"|"right"|"origin"|"axis.x"|"axis.y"|"axis.z"},
                    {kind:"sketch_entity",sketch:"f2",entity:"e1"}.
                    Faces project all boundary wires; pierce needs a plane crossing.
                    Associative by default: references follow source changes. fix:true pins
                    only converted references, not authored geometry. id_map entries:
                    "$<convert-index>", "$<convert-index>#1",… in ref order.
                    Then use e.g. coincident refs:["e1.p1","$<convert-index>.p"] for a vertex.

sketch_fillet       {"op":"sketch_fillet","sketch":"f1","e1":"e1","e2":"e2","radius":2}
                    Line-line corners only; trims lines, adds true arc and tangent relations.
sketch_trim         {"op":"sketch_trim","sketch":"f1","entity":"e3","point":[10,4],"keep_construction":true}
                    Remove the span under point; optionally retain it as construction.
sketch_mirror_entities {"op":"sketch_mirror_entities","sketch":"f1","entities":["e1"],"line":"e9"}
                    e9 must be a REAL construction line entity, not axis.v; adds symmetry.
sketch_pattern_entities {"op":"sketch_pattern_entities","sketch":"f1","entities":["e1"],"mode":"linear",
                        "count":3,"dir":[1,0],"spacing":12}
                    Circular: mode:"circular", count:4, center:[0,0], angle:360, equal_spacing:true
                    (replace dir/spacing). Count includes seed. Define copies, then check.

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
 {"op":"add_feature","feature":{"type":"sketch","name":"Holes","refs":{"plane":"plane.top"},"params":{"entities":[{"id":"e1","type":"circle","center":[24,14],"radius":3},{"id":"e2","type":"circle","center":[-24,14],"radius":3},{"id":"e3","type":"circle","center":[24,-14],"radius":3},{"id":"e4","type":"circle","center":[-24,-14],"radius":3}],"constraints":[{"id":"c1","type":"symmetric","refs":["e1","e2","axis.v"]},{"id":"c2","type":"symmetric","refs":["e1","e3","axis.h"]},{"id":"c3","type":"symmetric","refs":["e2","e4","axis.h"]},{"id":"c4","type":"distance","refs":["e1.center","e2.center"],"value":{"value":48,"expr":null}},{"id":"c5","type":"distance","refs":["e1.center","e3.center"],"value":{"value":28,"expr":null}},{"id":"c6","type":"radius","refs":["e1"],"value":{"value":3,"expr":null}}]}}},
 {"op":"sketch_fully_define","sketch":"$1"},
 {"op":"add_feature","feature":{"type":"extrude","name":"Drill","params":{"mode":"cut","end":"through_all"},"refs":{"profile":"$1"}}}
]

C — a vase as a SOLID OF REVOLUTION. The half-profile must be a CLOSED region: the curved outer
wall (a spline) plus a straight line back down the Z axis to close it. Revolve 360 about Z:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Profile","refs":{"plane":"plane.front"},"params":{"entities":[{"id":"e1","type":"spline","points":[[0,0],[15,0],[18,20],[10,45],[12,60],[0,60]]},{"id":"e2","type":"line","p1":[0,60],"p2":[0,0]}],"constraints":[{"id":"c1","type":"coincident","refs":["e1.start","e2.p2"]},{"id":"c2","type":"coincident","refs":["e1.end","e2.p1"]},{"id":"c3","type":"coincident","refs":["e2.p2","origin"]},{"id":"c4","type":"vertical","refs":["e2"]},{"id":"c5","type":"length","refs":["e2"],"value":{"value":60,"expr":null}}]}}},
 {"op":"sketch_fully_define","sketch":"$0"},
 {"op":"add_feature","feature":{"type":"revolve","name":"Body","params":{"mode":"boss","angle":{"value":360,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[8,30],"hint_index":0}],"axis":{"kind":"datum","feature":"axis.z"}}}}
]

D — a blended duct by LOFTING a circle into a square (two sketches on parallel planes):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Bottom","refs":{"plane":"plane.top"},"params":{"entities":[{"id":"e1","type":"circle","center":[0,0],"radius":20}],"constraints":[{"id":"c1","type":"coincident","refs":["e1.center","origin"]},{"id":"c2","type":"radius","refs":["e1"],"value":{"value":20,"expr":null}}]}}},
 {"op":"sketch_fully_define","sketch":"$0"},
 {"op":"add_feature","feature":{"type":"sketch","name":"Top","params":{"entities":[{"type":"rectangle","center":[0,0],"width":30,"height":30}],"offset":{"value":50,"expr":null}},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_fully_define","sketch":"$2"},
 {"op":"add_feature","feature":{"type":"loft","name":"Blend","params":{"mode":"boss"},"refs":{"sections":[{"kind":"region","sketch":"$0","centroid":[0,0],"hint_index":0},{"kind":"region","sketch":"$2","centroid":[0,0],"hint_index":0}]}}}
]

E — 6 bosses evenly around the Z axis: make ONE as a sketch+extrude boss (a primitive can't be
patterned), then circular-pattern that extrude feature about axis.z:
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Base","params":{"radius":{"value":40,"expr":null},"height":{"value":5,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"sketch","name":"BossSk","refs":{"plane":"plane.top"},"params":{"entities":[{"id":"e1","type":"circle","center":[30,0],"radius":4}],"constraints":[{"id":"c1","type":"point_on","refs":["e1.center","axis.h"]},{"id":"c2","type":"distance","refs":["origin","e1.center"],"value":{"value":30,"expr":null}},{"id":"c3","type":"radius","refs":["e1"],"value":{"value":4,"expr":null}}]}}},
 {"op":"sketch_fully_define","sketch":"$1"},
 {"op":"add_feature","feature":{"type":"extrude","name":"Boss","params":{"mode":"boss","end":"blind","depth":{"value":10,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$1","centroid":[30,0],"hint_index":0}]}}},
 {"op":"add_feature","feature":{"type":"circular_pattern","name":"Ring","params":{"count":6,"angle":{"value":360,"expr":null},"equal_spacing":true},"refs":{"axis":{"kind":"datum","feature":"axis.z"},"features":["$3"]}}}
]

----------------------------------------------------------------- TOOL SCOPE
request_tools — meta-op (not applied to the doc). Ask for more vocabulary mid-run:
  [{"op":"request_tools","classes":["full_ops"|"sheetmetal"|"weldments"|"surfacing"]}]
Domain classes append cheatsheets/sheetmetal.md, weldment.md, or surfacing.md on the next turn.
You already have the full op set on this sheet; prefer emitting real ops. A second full_ops
request is a no-op.
