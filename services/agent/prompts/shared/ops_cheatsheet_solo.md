DOC OPS CHEATSHEET (SOLO). Emit a JSON array of document ops. Units mm / degrees.
Dimension params are objects: {"value": <num>, "expr": null}. A later op refers to a feature
created earlier IN THE SAME ARRAY as "$<index>" (0-based op position); refer to existing tree
features by their id (e.g. "f1"). Reply with ONE fenced ```json array — no prose outside it.

----------------------------------------------------------------- ENVELOPE
[
 {"op":"add_feature","feature":{"type":"<type>","name":"<Name>","params":{...},"refs":{...}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{...}}
]

----------------------------------------------------------------- PRIMITIVES (op "add_feature")
box {length,width,height}   cylinder {radius,height}   sphere {radius}
cone {r1,r2,height}         torus {major_r,minor_r}     +optional {at:[x,y,z], centered:false}
`mode` "boss"|"cut" and `merge` combine with the running body (cylinder mode:"cut" bores a hole).
RELATIONAL `at`: `at:{"of":"face","side":"top"}` centres on a face; `at_offset:[dx,dy,dz]` nudges.

----------------------------------------------------------------- SKETCH + PROFILE
1) add_feature type "sketch", refs.plane = "plane.top" | "plane.front" | "plane.right"
   (NEVER "XY"/"XZ"/"YZ"). plane.top = XY (+Z up), plane.front = XZ, plane.right = YZ.
   ON AN EXISTING FACE: refs.plane = {"of":"face","side":"top"} — 2D origin is that face's centre.
   Optional `offset`:<mm> lifts along the face/datum normal.
   ⚠ BOSS on an existing part: sketch on the FACE, NOT "plane.top" (primitives are centred, so
     plane.top cuts the middle — a boss there extrudes INTO the part).
2) sketch_add_entity (sketch = "$<idx>"):
     circle {center:[x,y], radius}      line {p1:[x,y], p2:[x,y]}
     arc {center:[x,y], radius, start:<deg>, end:<deg>, ccw:<bool>}
     REGULAR POLYGON / HEX — ONE entity, never hand-draw sides:
       {"op":"sketch_add_entity","sketch":"$0","type":"hexagon","center":[0,0],"across_flats":16}
       type "polygon"/"ngon" needs "sides":N; named: hexagon, octagon, …
       size — exactly ONE of: "across_flats" (flat-to-flat), "circumradius", "inradius"/"apothem".
3) Solidify with ONE of:
   EXTRUDE  params {mode:"boss"|"cut", end:"blind"|"through_all", depth:<mm>},
            refs.profile = [{"kind":"region","sketch":"$<idx>","centroid":[x,y],"hint_index":0}]
   REVOLVE  params {mode, angle:{value:360,"expr":null}}, refs.profile = [region],
            refs.axis = {"kind":"datum","feature":"axis.x"|"axis.y"|"axis.z"}
            (profile must lie OUTSIDE / beside the axis).

----------------------------------------------------------------- HOLE (prefer over hand-cut circles)
hole — params {style:"simple"|"cbore"|"csink"|"tap", end:"through_all"|"blind",
        diameter, depth?(blind), cbore_d?/cbore_depth?, csink_d?/csink_angle?}.
        Size by `diameter` (mm) OR `"standard":"M3"` (clearance bore — M3→3.4mm; tap-drill when
        style:"tap"). For an "M3 clearance bore" just give {"standard":"M3"}.
        refs.face = {"of":"face","side":"top"}. Optional positions:[[u,v],..]; omit = centre.
        POSITIONS FRAME: origin = centroid of face outer boundary; u = +X in face; v = n×u.

----------------------------------------------------------------- DRESS
fillet  params {radius} refs.edges = {"of":"edge", ...}   (omit filters = ALL edges)
chamfer params {length} refs.edges = {...}
Query predicates (combine freely): "side":"top"|"bottom"|"left"|"right"|"front"|"back",
  "surf":"flat"|"round", "parallel":"z"|"x"|"y", "pick":"largest"|"smallest"|"inner"|"outer".

----------------------------------------------------------------- BOOLEAN
type "boolean" params.op: "fuse"|"cut"|"common" (aliases combine/subtract/intersect).
  Shorthand: params.a = "f1", params.b = "f3". Canonical: refs.targets/tools body selectors.
  Both bodies consumed; result must be ONE connected solid.

----------------------------------------------------------------- PATTERN
Seed in refs.features MUST be sketch-based (extrude/hole/revolve) — NOT a bare primitive.
linear_pattern  params {count1:<int>, spacing1:<dim>, count2?, spacing2?},
        refs.dir1 = {"kind":"datum","feature":"axis.x"}, refs.features = ["$3", ...].
circular_pattern params {count:<int>, angle:<dim, dflt 360>, equal_spacing?:true},
        refs.axis = {"kind":"datum","feature":"axis.z"}, refs.features = ["$2", ...].
`count*` are plain INTEGERS; spacings/angles are dim objects {"value":N,"expr":null}.

----------------------------------------------------------------- STANDARD PART (hardware macro)
add_feature type "standard_part" expands a catalog hardware part into normal editable tree
features (same generators as parts_make). PREFER this over hand-modeling stock hardware.
  {"op":"add_feature","feature":{"type":"standard_part","name":"Standoff",
    "params":{"family":"standoff_hex","thread":"M3","length_mm":22}}}
Params (mm): family (required) · size/thread ("M3", "1/4-20", bearing "608") · length_mm
  (screws/standoffs/rod/dowel/insert need it; nuts/washers/bearings ignore it).
Families: shcs, bhcs, fhcs, hex_bolt, hex_nut, washer_flat, washer_lock, standoff_round,
  standoff_hex, threaded_rod, dowel_pin, heat_set_insert, bearing.
Invalid family → error lists valid keys. Add custom features (knurl-ish chamfers, extra bores)
as ordinary ops AFTER the macro.

----------------------------------------------------------------- EDIT
edit_feature   {"op":"edit_feature","id":"f1","params":{"length":{"value":80,"expr":null}}}
delete_feature {"op":"delete_feature","id":"f3","cascade":true}
set_suppressed {"op":"set_suppressed","id":"f3","value":true}

----------------------------------------------------------------- FOOTGUNS (one line each)
- Dims are {"value":N,"expr":null} objects — bare numbers often fail validation on feature params.
- $n is the 0-based op index in THIS array, not a feature id; tree features use "f1", "f2", …
- Plane names are plane.top/front/right — never XY/XZ/YZ.
- Hex/nut/bolt-head: one hexagon entity with across_flats — never N separate lines.
- Hole diameter is a DIAMETER; M-series clearance = {"standard":"M3"} (not hand-computed radius).
- Pattern/mirror seeds must be sketch-based features; box/cylinder primitives are NOT patternable.
- Boss on a body: sketch on the face query, not plane.top through a centred primitive.
- Same-batch refs: after sketch at $0 and entity at $1, extrude profile sketch is "$0" (the feature).

----------------------------------------------------------------- EXAMPLE — hex standoff with bore
M3 clearance hex standoff, 16mm across flats, 22mm tall (canonical solo part):
```json
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Hex","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","type":"hexagon","center":[0,0],"across_flats":16},
 {"op":"add_feature","feature":{"type":"extrude","name":"Body","params":{"mode":"boss","end":"blind","depth":{"value":22,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[0,0],"hint_index":0}]}}},
 {"op":"add_feature","feature":{"type":"hole","name":"Bore","params":{"style":"simple","end":"through_all","standard":"M3"},"refs":{"face":{"of":"face","side":"top"}}}}
]
```

----------------------------------------------------------------- TOOL SCOPE
This is the SLIM tool set. If the task needs ops not listed here (sheet metal, weldments,
surfaces, lofts, sweeps, advanced patterns), reply with ONLY
[{"op":"request_tools","classes":["full_ops"]}] and you will receive the full op vocabulary
on the next turn. Domain extras: add "sheetmetal", "weldments", or "surfacing" to classes.
