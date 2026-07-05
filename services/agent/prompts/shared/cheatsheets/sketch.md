SKETCH. Create a sketch on a plane, then add entities. Coordinates are 2D on the plane; the plane is centered on the world origin. THIS is how you place geometry away from the origin (primitives can't be moved) - sketch entities sit at exactly the [x,y] you give them.

Op shape (a JSON ARRAY): first an `add_feature` of type `sketch` (refs.plane = "plane.top"|"plane.front"|"plane.right"), then `sketch_add_entity` ops referring to it as `"$0"`.

The reserved plane names are EXACTLY "plane.top" (the XY plane, +Z up), "plane.front" (XZ), "plane.right" (YZ). Do NOT write "XY"/"XZ"/"YZ" - they are rejected.

Entity `type` is one of: `circle` (center, radius), `line` (p1, p2), `arc` (center, radius, start, end, ccw), `point` (p), `spline` (points: [[x,y],...] - a smooth curve THROUGH the points, for organic / profile / cam outlines), `airfoil` (naca:"2412", chord, aoa - a NACA aerofoil section as one closed loop, for wings), `polygon` (a regular N-gon as ONE closed loop). There is NO "rect" entity - build a rectangle from 4 `line` entities.

A `polygon` is the way to make a HEXAGON / hex standoff / nut / bolt-head / any regular N-sided profile - NEVER draw it as a spline (an open spline makes no closed region and the extrude builds nothing). Shape: `{"type":"polygon","center":[0,0],"sides":6,"across_flats":12}`. Give `sides` (>=3) and EXACTLY ONE size: `across_flats` (the wrench/spanner size, flat-to-flat - the usual way to spec a hex), or `circumradius` (centre-to-vertex), or `inradius`. Optional `rotation` (degrees). NAMED shorthands set the side count for you (an explicit `sides` still overrides): `triangle`(3), `pentagon`(5), `hexagon`(6), `heptagon`(7), `octagon`(8), `nonagon`(9), `decagon`(10), `dodecagon`(12) - e.g. `{"type":"octagon","center":[0,0],"across_flats":20}`. The engine expands it into the N lines - do NOT also add the lines yourself; just extrude the sketch.

A spline closes a profile when its ends meet the other edges (e.g. a revolve half-profile = a spline outer wall + a line back down the axis). An `airfoil` entity is self-contained (it expands to the upper/lower splines + trailing-edge line) - just extrude it.

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

Example C - a NACA 2412 wing section (then EXTRUDE it for a wing):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"WingSec","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.front"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"airfoil","naca":"2412","chord":120,"aoa":2}}
]

3 common mistakes:
1. A sketch ALONE makes no solid - it must be extruded/cut/revolved/lofted in a LATER step. Do not expect volume yet.
2. Plane MUST be "plane.top"/"plane.front"/"plane.right" (a string), never "XY" or an object. Entity coords are 2D ([x,y]), never 3D.
3. No "rect" entity - a rectangle is 4 `line` entities; a round hole/post is a `circle`; a curved profile is a `spline`.

PROJECTING EXISTING GEOMETRY INTO A SKETCH ("Convert Entities"):
Use project_into_sketch(sketch, refs) to append edges/faces from the surrounding solid into an
existing sketch. This is the MCP equivalent of SolidWorks "Convert Entities" — it does NOT create
a new sketch (for that, use convert_face_to_sketch).

  refs accepts: a datum string ("front"/"top"/"right" → datum plane intersection line), a face sid
  ("F…" from describe_part), a side word ("bottom" etc.; note "front"/"top"/"right" are datums
  first), an edge sid ("E…"), a ref dict {"kind":"face","selector":{"sid":"…"}}, or a list of any mix.

  A face ref projects ALL its boundary wires (outer + hole loops) as entities. Oblique circles
  become ellipses. Each projected entity is pinned with a fix relation.

  ONE-SHOT (non-associative): converted entities do NOT update when the source model changes.
  Pass construction=True for blue reference-only geometry (excluded from extrude regions).

  Example — project the top face's outline into an existing sketch "s2":
  project_into_sketch("s2", "Fdb31d2")   # face sid from describe_part

  The underlying doc op is sketch_convert (see ops_cheatsheet for the raw op shape).
