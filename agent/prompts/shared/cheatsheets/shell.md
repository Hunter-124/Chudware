SHELL - hollow a solid to a wall thickness, optionally removing faces to make it open.

Op shape: one `add_feature` of type `shell`. params: `thickness` (dim object), `outward` (bool, default false = hollow inward). refs.remove = [face selector, ...] (faces to open; empty = a closed hollow shell).

A face selector: {"kind":"face","surf":"plane","centroid":[x,y,z],"axis":[nx,ny,nz],"area_or_len":<area>,"owner_feature":"<feature id>","hint_index":0}. A 40x30x10 box's top face center is [0,0,5], normal [0,0,1], area 1200.

Example A - hollow box f1 to 2mm walls, open the top face:
[
 {"op":"add_feature","feature":{"type":"shell","name":"Shell1","params":{"thickness":{"value":2,"expr":null},"outward":false},"refs":{"remove":[{"kind":"face","surf":"plane","centroid":[0,0,5],"axis":[0,0,1],"area_or_len":1200,"owner_feature":"f1","hint_index":0}]}}}
]

Example B - closed hollow shell (no removed faces), 1.5mm walls:
[
 {"op":"add_feature","feature":{"type":"shell","name":"Hollow","params":{"thickness":{"value":1.5,"expr":null},"outward":false},"refs":{"remove":[]}}}
]

3 common mistakes:
1. Shell BEFORE adding fillets/holes usually succeeds where after fails - shell early.
2. Thickness must be smaller than half the smallest dimension, or OCCT fails.
3. `remove` is an ARRAY of face selectors (possibly empty); a missing remove key still needs `[]`.
