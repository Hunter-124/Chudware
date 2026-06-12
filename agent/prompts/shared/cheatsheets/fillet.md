FILLET - round off edges with a radius. Reference each edge by a selector.

Op shape: one `add_feature` of type `fillet`. params: `radius` (dim object). refs.edges = [edge selector, ...].

An edge selector names ONE edge by midpoint + direction + length + owner: {"kind":"edge","surf":"line","centroid":[x,y,z],"axis":[ux,uy,uz],"area_or_len":<len>,"owner_feature":"<feature id>","hint_index":0}. Solids are origin-centered: a 40x30x10 box's +X top edge midpoint is [20,0,5], running along Y (axis [0,1,0]), length 30.

Example A - 3mm fillet on the +X top edge of box f1 (40x30x10):
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Fillet1","params":{"radius":{"value":3,"expr":null}},"refs":{"edges":[{"kind":"edge","surf":"line","centroid":[20,0,5],"axis":[0,1,0],"area_or_len":30,"owner_feature":"f1","hint_index":0}]}}}
]

Example B - fillet the 4 vertical edges (along Z) of a 40x30x10 box r=5:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Round","params":{"radius":{"value":5,"expr":null}},"refs":{"edges":[
  {"kind":"edge","surf":"line","centroid":[20,15,0],"axis":[0,0,1],"area_or_len":10,"owner_feature":"f1","hint_index":0},
  {"kind":"edge","surf":"line","centroid":[-20,15,0],"axis":[0,0,1],"area_or_len":10,"owner_feature":"f1","hint_index":1},
  {"kind":"edge","surf":"line","centroid":[20,-15,0],"axis":[0,0,1],"area_or_len":10,"owner_feature":"f1","hint_index":2},
  {"kind":"edge","surf":"line","centroid":[-20,-15,0],"axis":[0,0,1],"area_or_len":10,"owner_feature":"f1","hint_index":3}]}}}
]

3 common mistakes:
1. `owner_feature` must be the id of the feature whose edges you fillet (the box/cylinder before it), or the edge won't resolve.
2. Radius must be SMALLER than the thinnest wall it touches, or the build fails (auto-clamp may shrink it).
3. Give ALL selector fields (centroid, axis, area_or_len) - a partial selector resolves to the wrong edge or none.
