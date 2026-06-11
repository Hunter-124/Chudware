DOC OPS CHEATSHEET. You emit a JSON array of document ops. Units mm/degrees.
Dimension params are objects: {"value": <num>, "expr": null}. A later op refers to an object
created earlier IN THE SAME ARRAY as "$<index>" (0-based op position).

Primitive features (params are dimension objects):
  box     {length,width,height}    cylinder {radius,height}    sphere {radius}
  cone    {r1,r2,height}           torus    {major_r,minor_r}
Dress features reference edges/faces by SELECTOR:
  fillet  {radius}  refs.edges=[selector,...]
  chamfer {length}  refs.edges=[selector,...]
An edge selector names ONE edge by its midpoint + direction + length + owner. Give ALL fields so it
resolves: {"kind":"edge","surf":"line","centroid":[x,y,z],"axis":[ux,uy,uz],"area_or_len":<len>,
"owner_feature":"<feature id>","hint_index":<n>}. Solids are origin-centered: box(L,W,H) spans
-L/2..+L/2, so a 40x30x10 box has its +X top edge midpoint at [20,0,5] running along Y, length 30.

Example A - 40x30x10 box with a 3mm fillet on the +X top edge:
[
 {"op":"add_feature","feature":{"type":"box","name":"Box1",
   "params":{"length":{"value":40,"expr":null},"width":{"value":30,"expr":null},
             "height":{"value":10,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"fillet","name":"Fillet1",
   "params":{"radius":{"value":3,"expr":null}},
   "refs":{"edges":[{"kind":"edge","surf":"line","centroid":[20,0,5],"axis":[0,1,0],
                     "area_or_len":30,"owner_feature":"$0","hint_index":0}]}}}
]

Example B - sketch a rectangle then extrude it 8mm (sketch on the XY datum plane):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Sketch1",
   "params":{"entities":[],"constraints":[]},"refs":{"plane":"XY"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"rect",
   "corner":[-15,-10],"width":30,"height":20}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Extrude1",
   "params":{"mode":"new","end":"blind","depth":{"value":8,"expr":null}},
   "refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[0,0],"hint_index":0}]}}}
]

Example C - a 60x40x6 plate with a 2x2 grid of M3 holes (escape hatch via one script feature):
[
 {"op":"add_feature","feature":{"type":"script","name":"Plate",
   "params":{"code":"result = Box(60,40,6)\nfor x,y in [(-22,-14),(22,-14),(-22,14),(22,14)]:\n    result -= Pos(x,y,0)*Cylinder(radius=1.7,height=8)"}}}
]
