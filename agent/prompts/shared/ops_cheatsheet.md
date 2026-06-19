DOC OPS CHEATSHEET. You emit a JSON array of document ops. Units mm/degrees.
Dimension params are objects: {"value": <num>, "expr": null}. A later op refers to a feature
created earlier IN THE SAME ARRAY as "$<index>" (0-based op position); refer to existing tree
features by their id (e.g. "f1").

ADD geometry (op "add_feature"):
  Primitives (ALWAYS centered on the origin - cannot be offset or counted):
    box {length,width,height}   cylinder {radius,height}   sphere {radius}
    cone {r1,r2,height}         torus {major_r,minor_r}
    A cylinder with params.mode="cut" bores a hole on the Z axis (make height > the part).
  Off-center / custom / repeated geometry -> a sketch + extrude:
    sketch  refs.plane = "plane.top" | "plane.front" | "plane.right"   (NEVER "XY")
            then sketch_add_entity ops; entities are circle/line/arc only (a rectangle = 4 lines).
    extrude params {mode:"boss"|"cut", end:"blind"|"through_all", depth}, refs.profile =
            [{"kind":"region","sketch":"<sketch id>","centroid":[0,0],"hint_index":0}] (extrudes/
            cuts ALL the sketch's regions regardless of which centroid you give).
  Dress an EXISTING body - name the edges/faces with a geometry QUERY (you need not click):
    fillet  {radius}  refs.edges = {"of":"edge", ...}     chamfer {length} refs.edges = {...}
    shell   {thickness} refs.remove = {"of":"face","side":"+z"}   (omit remove = closed shell)
    Query predicates: {"of":"edge"|"face"} all; add "side":"+z"/"-z"/... (the top/bottom/.. set),
    "parallel":[x,y,z] (edges parallel / faces whose normal is parallel), "surf":"circle"/"plane"/..,
    "near":[x,y,z]+"count". Discover geometry with the doc_query tool first if unsure.

EDIT an existing feature (no geometry selectors needed):
  edit_feature   {"op":"edit_feature","id":"f1","params":{"length":{"value":80,"expr":null}}}
                 - patches params (and/or refs); the value you give replaces that param.
  set_suppressed {"op":"set_suppressed","id":"f3","value":true}   - turn a feature off/on.
  delete_feature {"op":"delete_feature","id":"f3","cascade":true} - remove it (cascade drops
                 features that depend on it; without cascade a depended-on feature is blocked).
  rename         {"op":"rename","id":"f1","name":"BasePlate"}

Example A - 40x40x20 block, then round ALL its edges 3mm:
[
 {"op":"add_feature","feature":{"type":"box","name":"Block","params":{"length":{"value":40,"expr":null},"width":{"value":40,"expr":null},"height":{"value":20,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"fillet","name":"Round","params":{"radius":{"value":3,"expr":null}},"refs":{"edges":{"of":"edge"}}}}
]

Example B - a 60x40 plate 6mm thick with four 3mm-radius corner holes (sketch circles + extrude cut):
[
 {"op":"add_feature","feature":{"type":"box","name":"Plate","params":{"length":{"value":60,"expr":null},"width":{"value":40,"expr":null},"height":{"value":6,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"sketch","name":"Holes","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[24,14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[24,-14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[-24,14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[-24,-14],"radius":3}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Drill","params":{"mode":"cut","end":"through_all"},"refs":{"profile":[{"kind":"region","sketch":"$1","centroid":[0,0],"hint_index":0}]}}}
]

Example C - edit an existing part: make box f1 80mm long and chamfer its top rim 1mm:
[
 {"op":"edit_feature","id":"f1","params":{"length":{"value":80,"expr":null}}},
 {"op":"add_feature","feature":{"type":"chamfer","name":"Rim","params":{"length":{"value":1,"expr":null}},"refs":{"edges":{"of":"edge","side":"+z"}}}}
]
