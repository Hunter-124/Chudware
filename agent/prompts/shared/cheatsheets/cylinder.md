CYLINDER primitive - a round solid. Centered on the origin by default: spans -height/2..+height/2 in Z, radius r about the Z axis. Add an `at` param to PLACE it somewhere else.

params: `radius`, `height` (dim objects). Optional `mode` ("boss" add | "cut" subtract). Optional `at` [x,y,z] - the center of the cylinder (a plain editable param a human can audit/change later). For a through-cut make height > the part so it fully passes.

Use a cylinder for any round feature: a central bore (no `at`), an OFF-CENTER hole (`at` at the hole center, mode "cut"), a post/boss (mode "boss"). Each cylinder is ONE position; for several holes emit several cylinder cuts, one per `at` (each stays an independent, editable feature). For a dense/grid pattern, prefer sketch circles + extrude-cut, or a pattern feature.

Example A - r=8, h=20 post centered on the origin:
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Post","params":{"radius":{"value":8,"expr":null},"height":{"value":20,"expr":null}}}}
]

Example B - a 3mm-radius hole bored at the corner [18,8] of a plate (cut, taller than the wall):
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Hole1","params":{"radius":{"value":3,"expr":null},"height":{"value":30,"expr":null},"mode":"cut","at":[18,8,0]}}}
]

3 common mistakes:
1. radius/height are dim OBJECTS {"value":n,"expr":null}, not bare numbers. `at` is a plain [x,y,z] list.
2. For a through-hole make the cylinder LONGER than the wall and use mode "cut"; an exact-length cut may leave a sliver.
3. Without `at` the cylinder is on the Z axis through the origin - several cuts without `at` stack and remove no material. Give each its own `at`.
