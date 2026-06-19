CYLINDER primitive - a round solid directly. ALWAYS origin-centered: spans -height/2..+height/2 in Z, radius r about the Z axis. It CANNOT be moved and has NO count - one cylinder, on the Z axis, every time.

Use a cylinder ONLY for a single round feature centered on the Z axis (a central bore, a central post, the main body of a round part). For a hole anywhere ELSE, or for MORE THAN ONE hole, do NOT use cylinders - sketch the circle(s) at their [x,y] on plane.top and extrude-CUT (see the sketch + extrude cheat-sheets). N cylinders all land on top of each other at the origin.

Op shape: one `add_feature` of type `cylinder`. params: `radius`, `height` (dim objects). Optional `mode` ("boss"|"cut").

Example A - r=8, h=20 post:
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Cyl1","params":{"radius":{"value":8,"expr":null},"height":{"value":20,"expr":null}}}}
]

Example B - bore a 3mm-radius hole through (cut, taller than the wall so it passes through):
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Bore1","params":{"radius":{"value":3,"expr":null},"height":{"value":30,"expr":null},"mode":"cut"}}}
]

3 common mistakes:
1. radius/height are dim OBJECTS {"value":n,"expr":null}, not bare numbers.
2. For a through-hole make the cylinder LONGER than the wall and use mode "cut"; an exact-length cut may leave a sliver.
3. A cylinder CANNOT be offset - it is always on the Z axis through the origin. Do NOT emit several cylinders for several holes (they stack at the origin and the cut removes no material). Multiple/off-center holes = sketch circles + extrude-cut.
