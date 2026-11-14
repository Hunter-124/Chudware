PRIMITIVES - box / cylinder / sphere / cone / torus (the fast path; no sketch needed).

params (dim objects {"value":n,"expr":null}):
  box {length (X), width (Y), height (Z)}   cylinder {radius, height}   sphere {radius}
  cone {r1, r2, height}                     torus {major_r, minor_r}
Placement keys (feature level or in params): at, at_offset, axis, centered, mode, merge.
Any other feature-level key on a primitive is refused and the error lists the accepted ones.

PLACEMENT - a primitive is built CENTRED ON THE ORIGIN, then placed:
  at:[x,y,z]   the CENTRE of the primitive (a cylinder spans at.z-height/2 .. at.z+height/2
               along its axis), not its base.
  at:{query}   e.g. {"of":"face","side":"top"} - centres it on the first face/edge the
               query matches on the CURRENT body (that face's centroid).
  at_offset    [dx,dy,dz] world-axis nudge added after `at`.
  axis         [x,y,z] or "+x"/"-x"/"+y"/"-y"/"+z"/"-z" - where local +Z points. Normalised;
               a zero-length axis is refused. Default +Z. A sphere ignores it.
  centered     false = box: its min CORNER sits at `at`; cylinder/cone: its BASE centre sits
               at `at` and it grows along +axis. Sphere and torus stay centred (no effect).
  Order: build centred -> rotate +Z onto `axis` about the origin -> centered:false base
  offset along the rotated axis -> translate by `at` -> add `at_offset`.

BODY RULES (mode / merge):
  mode "boss" (default; alias "add") fuses the primitive into EVERY live body it touches.
          If it touches none it becomes a new body. A boss that adds no volume is refused
          ("boss added no material": it is buried inside the body).
  mode "cut" subtracts from the CURRENT body only = the most recently built body that is
          still live. It never reaches other bodies. A cut that removes no volume is
          refused and the error prints both bounding boxes.
  mode "new" or merge:false always starts a separate body (merge:false overrides mode).
  A later explicit `boolean` needs two separate bodies; build its operands with merge:false
  (a boss already fused them, so the boolean has nothing left to combine).
  After a cut or boss, check body_count and volume_mm3 in the add_features reply.

Primitives are NOT patternable or mirrorable seeds; use sketch + extrude for repeated
features. plane.top passes through the origin, so a box with no `at` is cut in half by it:
sketch a boss on the face ({"of":"face","side":"top"}), not on plane.top (cad_guide("planes")).

Example - 60x40x10 plate with its base on z=0, then a separate r=5 h=30 pin beside it:
[
 {"op":"add_feature","feature":{"type":"box","name":"Plate","params":{"length":{"value":60,"expr":null},"width":{"value":40,"expr":null},"height":{"value":10,"expr":null}},"at":[0,0,0],"centered":false}},
 {"op":"add_feature","feature":{"type":"cylinder","name":"Pin","params":{"radius":{"value":5,"expr":null},"height":{"value":30,"expr":null}},"at":[100,0,0],"centered":false,"merge":false}}
]
(centered:false puts the box's min corner at the origin: it spans 0..60, 0..40, 0..10.)

Per-type sheets: cad_guide("box"), cad_guide("cylinder"). Move a finished body: cad_guide("transform").
