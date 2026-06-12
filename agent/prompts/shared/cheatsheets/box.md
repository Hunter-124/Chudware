BOX primitive - a solid block directly (the fast path; no sketch needed). Origin-centered: box(L,W,H) spans -L/2..+L/2 in X, -W/2..+W/2 in Y, -H/2..+H/2 in Z.

Op shape: one `add_feature` of type `box`. params: `length`, `width`, `height` (each a dim object {"value":n,"expr":null}). X=length, Y=width, Z=height.

Example A - 60x40x10 block:
[
 {"op":"add_feature","feature":{"type":"box","name":"Box1","params":{"length":{"value":60,"expr":null},"width":{"value":40,"expr":null},"height":{"value":10,"expr":null}}}}
]

Example B - a thin 80x80x3 plate:
[
 {"op":"add_feature","feature":{"type":"box","name":"Plate","params":{"length":{"value":80,"expr":null},"width":{"value":80,"expr":null},"height":{"value":3,"expr":null}}}}
]

3 common mistakes:
1. Each dimension is a dim OBJECT {"value":n,"expr":null}, never a bare number.
2. The box is CENTERED on the origin - its top face is at z=+height/2, not z=height. Derive feature positions from the half-dimensions.
3. Use `box` for a rectangular block; do not sketch+extrude unless the request needs a custom profile.
