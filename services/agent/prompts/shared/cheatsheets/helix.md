HELIX — a curve feature for springs, coils, and threads. A helix emits NO solid by itself.
Use it as refs.path in a SWEEP to make a spring wire, coil, or thread form.

REQUIRED params: pitch (mm per revolution), height (total axial travel mm).
REQUIRED ref: base = a datum plane ("plane.top" / "plane.front" / "plane.right") that sets the
origin and axis of the helix (the coil grows along the plane's normal, i.e. +Z for plane.top).

OPTIONAL params:
  radius       — coil radius (default 10mm). Place the sweep PROFILE circle centered at (radius, 0)
                 on the plane that contains the helix axis (e.g. plane.front if base=plane.top).
  taper        — taper half-angle in degrees (narrows toward the top; 0 = straight cylinder).
  turns        — number of turns (overrides height = turns × pitch).
  start_angle  — angular offset of the first point (degrees; default 0).
  ccw          — true/false (default true = counter-clockwise when viewed from +normal).
  shape:"spiral" — flat Archimedean spiral in the base plane instead of a helix. Set `turns`
                    (not height); `start_radius` sets the inner radius (defaults to `radius`/0).

VARIABLE PITCH — omit pitch and height; instead give pitch_table as a list of rows:
  Each row is a mapping {turn:<float>, pitch:<mm>} or bare [turn, pitch] array.
  Optional dia (or radius) per row to taper the coil radius as well.
  Rows are sorted by `turn`; pitch and radius interpolate linearly between rows.
  Needs at least 2 rows spanning a positive number of turns.
  Example: [{turn:0, pitch:2}, {turn:3, pitch:6}, {turn:5, pitch:2}]

TYPICAL USAGE — compression spring (wire radius 1.5mm, coil radius 8mm, 3 turns, pitch 4mm):

1. Sketch: circle r=1.5 centered at (8,0) on plane.front (the helix start point).
2. Helix: base=plane.top, radius=8, pitch=4, height=12 (= 3 turns × 4mm).
3. Sweep: profile=$0 (the circle sketch), path=$1 (the helix).

Full op JSON:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"WireProf",
  "params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.front"}}},
 {"op":"sketch_add_entity","sketch":"$0",
  "entity":{"type":"circle","center":[8,0],"radius":1.5,"id":"e1","construction":false}},
 {"op":"add_feature","feature":{"type":"helix","name":"CoilPath",
  "params":{"pitch":{"value":4,"expr":null},"height":{"value":12,"expr":null},
            "radius":{"value":8,"expr":null}},
  "refs":{"base":"plane.top"}}},
 {"op":"add_feature","feature":{"type":"sweep","name":"Spring",
  "params":{"mode":"boss"},
  "refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[8,0],"hint_index":0}],
          "path":"$2"}}}
]

THREAD FORM — same idea: profile = the thread cross-section (triangle/trapezoid), path = a helix
whose radius matches the thread's pitch radius. Sweep mode "cut" on a cylinder shaft = external
thread; mode "boss" on a hollow bore sketch = internal thread form.
