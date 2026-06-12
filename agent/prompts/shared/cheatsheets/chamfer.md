CHAMFER - bevel edges by a setback length. Reference each edge by a selector.

Op shape: one `add_feature` of type `chamfer`. params: `length` (dim object - NOT radius). refs.edges = [edge selector, ...].

An edge selector: {"kind":"edge","surf":"line","centroid":[x,y,z],"axis":[ux,uy,uz],"area_or_len":<len>,"owner_feature":"<feature id>","hint_index":0}. A 40x30x10 box's +X top edge midpoint is [20,0,5], along Y, length 30.

Example A - 2mm chamfer on the +X top edge of box f1 (40x30x10):
[
 {"op":"add_feature","feature":{"type":"chamfer","name":"Chamfer1","params":{"length":{"value":2,"expr":null}},"refs":{"edges":[{"kind":"edge","surf":"line","centroid":[20,0,5],"axis":[0,1,0],"area_or_len":30,"owner_feature":"f1","hint_index":0}]}}}
]

Example B - chamfer a top rim edge 1mm:
[
 {"op":"add_feature","feature":{"type":"chamfer","name":"Rim","params":{"length":{"value":1,"expr":null}},"refs":{"edges":[{"kind":"edge","surf":"line","centroid":[0,15,5],"axis":[1,0,0],"area_or_len":40,"owner_feature":"f1","hint_index":0}]}}}
]

3 common mistakes:
1. Chamfer uses `length` (the setback), NOT `radius`. Using radius is rejected.
2. `owner_feature` must be the id of the feature being chamfered.
3. Keep length smaller than the adjacent wall/face, or OCCT fails.
