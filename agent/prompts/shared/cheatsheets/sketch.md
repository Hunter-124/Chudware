SKETCH. Create a sketch on a plane, then add entities. Coordinates are 2D on the plane; the plane is centered on the world origin. THIS is how you place geometry away from the origin (primitives can't be moved) - sketch entities sit at exactly the [x,y] you give them.

Op shape (a JSON ARRAY): first an `add_feature` of type `sketch` (refs.plane = "plane.top"|"plane.front"|"plane.right"), then `sketch_add_entity` ops referring to it as `"$0"`.

The reserved plane names are EXACTLY "plane.top" (the XY plane, +Z up), "plane.front" (XZ), "plane.right" (YZ). Do NOT write "XY"/"XZ"/"YZ" - they are rejected.

Entity `type` is one of: `circle` (center, radius), `line` (p1, p2), `arc` (center, radius, start, end, ccw), `point` (p). There is NO "rect" entity - build a rectangle from 4 `line` entities.

Example A - circle r=8 at the origin on the top plane:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Sketch1","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[0,0],"radius":8}}
]

Example B - four 3mm-radius circles at the corners of a 60x60 plate (the right way to lay out a bolt pattern - then extrude-CUT them):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Bolts","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[24,24],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[24,-24],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[-24,24],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[-24,-24],"radius":3}}
]

3 common mistakes:
1. A sketch ALONE makes no solid - it must be extruded/cut in the NEXT step. Do not expect volume yet.
2. Plane MUST be "plane.top"/"plane.front"/"plane.right" (a string), never "XY" or an object. Entity coords are 2D ([x,y]), never 3D.
3. No "rect" entity - a rectangle is 4 `line` entities; a round hole/post is a `circle`.
