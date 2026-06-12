CYLINDER primitive - a round solid directly. Origin-centered: spans -height/2..+height/2 in Z, radius r about the Z axis.

Op shape: one `add_feature` of type `cylinder`. params: `radius`, `height` (dim objects). Optional `mode` ("boss"|"cut") and `at` ref for placement.

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
3. Cylinder is centered on the origin; to move it use the `at` ref or place via the request's coordinates, not by assuming a base at z=0.
