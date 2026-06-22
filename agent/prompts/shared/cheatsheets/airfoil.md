AIRFOIL — drop a parametric aerofoil section into a sketch as ONE entity; it expands to the
upper + lower surface splines (plus a trailing-edge line for an open TE) as a closed loop you then
EXTRUDE (a straight wing) or LOFT between two airfoil sketches (a tapered/twisted wing).

Entity shape (inside a sketch, via sketch_add_entity):
  {"type":"airfoil", "naca":"2412", "chord":100, "aoa":0, "points":60, "closedTE":true}
  - naca : a 4-digit ("2412") or 5-digit ("23012") NACA code. OR use "dat":"<path>" for a
           Selig/Lednicer .dat file instead of naca.
  - chord: chord length in mm.  aoa: angle of attack in degrees (nose-up), default 0.
  - The leading edge sits at the sketch origin; the chord runs along +X.

Example — a straight wing: a NACA 2412 section, 120mm chord, extruded 400mm:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"WingSec","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.front"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"airfoil","naca":"2412","chord":120,"aoa":2}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Wing","params":{"mode":"boss","end":"blind","depth":{"value":400,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[60,0],"hint_index":0}]}}}
]

For a TAPERED wing, make a root airfoil sketch (big chord) and a tip airfoil sketch (smaller chord,
on a plane offset along the span) and LOFT between the two regions.

3 common mistakes:
1. The airfoil is ONE entity — do not also add the splines yourself; the engine expands it.
2. Put it on a plane whose normal is the SPAN direction (extrude/loft then runs along the span).
3. Use loft between TWO airfoil sketches for taper/twist; a single airfoil + extrude is a constant wing.
