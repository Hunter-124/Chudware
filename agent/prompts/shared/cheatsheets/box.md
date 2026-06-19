BOX primitive - a solid block (the fast path; no sketch needed). Centered on the origin by default: box(L,W,H) spans -L/2..+L/2 in X, -W/2..+W/2 in Y, -H/2..+H/2 in Z. Add an `at` param to PLACE it elsewhere.

params: `length`, `width`, `height` (each a dim object {"value":n,"expr":null}; X=length, Y=width, Z=height). Optional `mode` ("boss" add | "cut" subtract). Optional `at` [x,y,z] = the box CENTER (a plain editable param a human can audit/change). Optional `centered` (bool, default true): set false to put the box's min CORNER at `at` instead of its center.

Place a box to make a step/boss/pocket off to one side. Note: for an L/T/U outline a single sketch of `line` entities + extrude is still cleaner than stacking placed boxes, but two boxes at different `at` positions also work and stay individually editable.

Example A - 60x40x10 block centered on the origin:
[
 {"op":"add_feature","feature":{"type":"box","name":"Box1","params":{"length":{"value":60,"expr":null},"width":{"value":40,"expr":null},"height":{"value":10,"expr":null}}}}
]

Example B - a 20x20x10 boss sitting on top at one end, centered at [20,0,10]:
[
 {"op":"add_feature","feature":{"type":"box","name":"Boss","params":{"length":{"value":20,"expr":null},"width":{"value":20,"expr":null},"height":{"value":10,"expr":null},"mode":"boss","at":[20,0,10]}}}
]

3 common mistakes:
1. Each dimension is a dim OBJECT {"value":n,"expr":null}; `at` is a plain [x,y,z] list.
2. Without `at` the box is centered on the origin (top face at z=+height/2). Two boxes with NO `at` both center on the origin and give a plus/cross, not an L - give them different `at`, or sketch+extrude the outline.
3. `at` is the box CENTER unless you set `centered:false` (then it is the min corner).
