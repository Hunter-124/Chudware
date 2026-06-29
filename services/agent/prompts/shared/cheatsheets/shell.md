SHELL - hollow a solid to a wall thickness, optionally opening faces.

Op shape: one `add_feature` of type `shell`. params: `thickness` (dim object), `outward` (bool, default false = hollow inward). refs.remove names the faces to OPEN (omit for a closed hollow shell).

BEST WAY to name the open face(s) - a geometry QUERY under refs.remove:
- {"of":"face","side":"+z"}      open the TOP face (the usual "open box")   ("-z"=bottom, etc.)
- {"of":"face","surf":"plane","side":"+z"}  same, restricted to planar faces
- {"of":"face","near":[x,y,z],"count":1}    open the face nearest a point

Example A - hollow the current body to 2mm walls, open the top:
[
 {"op":"add_feature","feature":{"type":"shell","name":"Shell1","params":{"thickness":{"value":2,"expr":null},"outward":false},"refs":{"remove":{"of":"face","side":"+z"}}}}
]

Example B - closed hollow shell (no opening), 1.5mm walls:
[
 {"op":"add_feature","feature":{"type":"shell","name":"Hollow","params":{"thickness":{"value":1.5,"expr":null},"outward":false}}}
]

3 common mistakes:
1. Shell BEFORE adding fillets/holes usually succeeds where after fails - shell early.
2. Thickness must be smaller than half the smallest dimension, or the build fails.
3. The query is an OBJECT under refs.remove ({"of":"face",...}); omit refs.remove entirely for a closed shell.

UNSUPPORTED on curved/freeform bodies: shell works on PRISMATIC (box-like) solids. It is NOT
reliable on a LOFT / SWEEP / REVOLVE body (B-spline / freeform faces) - the offset self-
intersects on curved skins and the op errors ("shell is not supported on lofted ... bodies").
To make a hollow CURVED body, build the wall thickness INTO the profile instead of shelling
afterward: sketch the wall cross-section as a thin closed loop (an inner curve + an outer curve
offset from it, closed at the ends) and LOFT or REVOLVE that - you get a hollow body directly.
See revolve.md "hollow body" for the ring-profile recipe.
