SKETCH (rect / circle). Create a sketch on a plane, then add entities. Coordinates are 2D on the plane; the plane is centered on the world origin.

Op shape (a JSON ARRAY): first an `add_feature` of type `sketch` (refs.plane = "XY"|"XZ"|"YZ"), then `sketch_add_entity` ops referring to it as `"$0"`.

Example A - 30x20 rectangle on XY (corner-based; centered at origin -> corner [-15,-10]):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Sketch1","params":{"entities":[],"constraints":[]},"refs":{"plane":"XY"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"rect","corner":[-15,-10],"width":30,"height":20}}
]

Example B - circle r=8 centered on XY:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Sketch1","params":{"entities":[],"constraints":[]},"refs":{"plane":"XY"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[0,0],"radius":8}}
]

3 common mistakes:
1. A sketch ALONE makes no solid - it must be extruded/revolved in the NEXT step. Do not expect volume yet.
2. `corner` is the rectangle's MIN corner, not its center; to center a WxH rect put corner at [-W/2,-H/2].
3. Plane is "XY" (a string), not an object. Entity coords are 2D ([x,y]), never 3D.
