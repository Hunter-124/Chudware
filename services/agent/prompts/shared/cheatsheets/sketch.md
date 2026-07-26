SKETCH. Create a sketch on a plane, then add entities. Coordinates are 2D on the plane; the plane is centered on the world origin. THIS is how you place geometry away from the origin (primitives can't be moved) - sketch entities sit at exactly the [x,y] you give them.

Op shape (a JSON ARRAY): first an `add_feature` of type `sketch` (refs.plane = "plane.top"|"plane.front"|"plane.right"), then `sketch_add_entity` ops referring to it as `"$0"`.

The reserved plane names are "plane.top" (the XY plane, +Z up), "plane.front" (XZ), "plane.right" (YZ). Write those. "XY"/"XZ"/"YZ" and bare "top"/"front"/"right" are ALSO accepted and rewritten to the reserved name for you (the rewrite comes back in `interpreted`), so a plane name never costs you a turn — but the four-word canonical spelling is what the engine stores.

Entity `type` is one of: `circle` (center, radius), `line` (p1, p2), `arc` (center, radius, start, end, ccw), `point` (p), `rectangle` (center+width+height OR corner1+corner2 — PREFERRED over 4 lines; expands to a closed loop), `spline` (points: [[x,y],...] - a smooth curve THROUGH the points, for organic / profile / cam outlines), `airfoil` (naca:"2412", chord, aoa - a NACA aerofoil section as one closed loop, for wings), `polygon` (a regular N-gon as ONE closed loop).

A `polygon` is the way to make a HEXAGON / hex standoff / nut / bolt-head / any regular N-sided profile - NEVER draw it as a spline (an open spline makes no closed region and the extrude builds nothing). Shape: `{"type":"polygon","center":[0,0],"sides":6,"across_flats":12}`. Give `sides` (>=3) and EXACTLY ONE size: `across_flats` (the wrench/spanner size, flat-to-flat - the usual way to spec a hex), or `circumradius` (centre-to-vertex), or `inradius`. Optional `rotation` (degrees). NAMED shorthands set the side count for you (an explicit `sides` still overrides): `triangle`(3), `pentagon`(5), `hexagon`(6), `heptagon`(7), `octagon`(8), `nonagon`(9), `decagon`(10), `dodecagon`(12) - e.g. `{"type":"octagon","center":[0,0],"across_flats":20}`. The engine expands it into the N lines - do NOT also add the lines yourself; just extrude the sketch.

A spline closes a profile when its ends meet the other edges (e.g. a revolve half-profile = a spline outer wall + a line back down the axis). An `airfoil` entity is self-contained (it expands to the upper/lower splines + trailing-edge line) - just extrude it.

A SKETCH IS NOT A SOLID. Every example below ends with the feature that turns it into one, because that is the step models most often leave out. The extrude op:
  {"op":"add_feature","feature":{"type":"extrude","name":"Body",
     "params":{"mode":"boss","depth":10},"refs":{"profile":"$0"}}}
  params.mode  "boss" fuses into the current body (the default if you omit it), "cut" subtracts, "new" makes a separate body.
  params.depth how far, in mm. Optional params.mid_plane:true grows BOTH ways from the sketch plane.
  refs.profile the sketch: "$0" for one added earlier in this SAME batch, or its "fN" id. A bare string is fine — a one-element list ["$0"] is the same thing.
  revolve is the same shape with params.angle and refs.axis; loft takes refs.sections; sweep takes refs.profile + refs.path.

Example A - a r=8 x 10mm tall cylinder, sketch to solid:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Sketch1","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[0,0],"radius":8}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Post","params":{"mode":"boss","depth":10},"refs":{"profile":"$0"}}}
]

Example B - four 3mm-radius bolt holes CUT through a 60x60x6 plate (the right way to lay out a bolt pattern). Note the cut extrude at the end and how it reaches "$1", the sketch, not "$0":
[
 {"op":"add_feature","feature":{"type":"box","name":"Plate","params":{"length":60,"width":60,"height":6}}},
 {"op":"add_feature","feature":{"type":"sketch","name":"Bolts","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[24,24],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[24,-24],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[-24,24],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[-24,-24],"radius":3}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Holes","params":{"mode":"cut","depth":20,"mid_plane":true},"refs":{"profile":"$1"}}}
]

Example C - a NACA 2412 wing section extruded into a 400mm wing:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"WingSec","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.front"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"airfoil","naca":"2412","chord":120,"aoa":2}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Wing","params":{"mode":"new","depth":400},"refs":{"profile":"$0"}}}
]

3 common mistakes:
1. Stopping at the sketch. A sketch ALONE makes no solid and reports no volume - it must be extruded/cut/revolved/lofted. Put that op in the SAME batch, as every example above does.
2. Entity coords are 2D ([x,y]) on the plane, never 3D. (The plane NAME is forgiving - see above - but the coordinates are not.)
3. Prefer `rectangle` (center+width+height) over 4 hand-drawn lines; a round hole/post is a `circle`; a curved profile is a `spline`. 4 lines still work if you already have corners pinned.

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
