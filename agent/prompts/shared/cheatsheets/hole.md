HOLE - a standards-aware hole (simple/cbore/csink/tap) on a face at sketched point positions.

Op shape: one `add_feature` of type `hole`. params: `style` ("simple"|"cbore"|"csink"|"tap"), `standard` ("M2".."M12"|"custom"), `diameter` (dim, custom only), `end` ("through_all"|"blind"), `depth` (dim, blind). refs.face = a planar face selector; refs.positions = a sketch of points (or positions_sketch = sketch feature id).

A face selector: {"kind":"face","surf":"plane","centroid":[x,y,z],"axis":[nx,ny,nz],"area_or_len":<area>,"owner_feature":"<feature id>","hint_index":0}.

Example A - M3 through-hole on box f1's top face, points from sketch f2:
[
 {"op":"add_feature","feature":{"type":"hole","name":"Hole1","params":{"style":"simple","standard":"M3","end":"through_all"},"refs":{"face":{"kind":"face","surf":"plane","centroid":[0,0,5],"axis":[0,0,1],"area_or_len":2400,"owner_feature":"f1","hint_index":0},"positions_sketch":"f2"}}}
]

Example B - custom 5mm diameter blind hole 8mm deep:
[
 {"op":"add_feature","feature":{"type":"hole","name":"Bore","params":{"style":"simple","standard":"custom","diameter":{"value":5,"expr":null},"end":"blind","depth":{"value":8,"expr":null}},"refs":{"face":{"kind":"face","surf":"plane","centroid":[0,0,5],"axis":[0,0,1],"area_or_len":2400,"owner_feature":"f1","hint_index":0},"positions_sketch":"f2"}}}
]

3 common mistakes:
1. A hole needs a POINTS SKETCH on its face (positions_sketch) - create that sketch first, then the hole.
2. Standard "M3" sets the diameter automatically; only set `diameter` when standard is "custom".
3. For a simple round bore without standards, a `cylinder` with mode "cut" is often simpler than `hole`.
