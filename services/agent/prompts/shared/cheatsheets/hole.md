HOLE - a standards-aware hole (simple/cbore/csink/tap) on a face at sketched point positions.

Op shape: one `add_feature` of type `hole`. params: `style` ("simple"|"cbore"|"csink"|"tap"), `standard` ("M2".."M12"|"custom"), `diameter` (dim, custom only), `end` ("through_all"|"blind"), `depth` (dim, blind). refs.face = a planar face selector; refs.positions = a sketch of points (or positions_sketch = sketch feature id).

refs.face is EASIEST as a plain-word geometry QUERY (no clicking, no coordinates): {"of":"face","side":"top"} = the top face ("bottom"/"left"/"right"/"front"/"back" too). A clicked face selector also works: {"kind":"face","surf":"plane","centroid":[x,y,z],"axis":[nx,ny,nz],"area_or_len":<area>,"owner_feature":"<feature id>","hint_index":0}.

POSITIONS without a sketch: pass params `positions`:[[u,v],...] — mm in the face's own frame, origin = face CENTRE. Omit positions entirely = ONE hole at the face centre. Use `positions_sketch` only when the points come from a real sketch.

WORLD placement: pass params `world_points`:[[x,y,z],...] — world mm points on each bore's AXIS (on the face is clearest); the engine projects them along the face normal and reports how far it moved them. With a word query (`{"of":"face","side":"top"}`) each axis drills whichever face facing that way it hits first (e.g. several boss tops in one feature); with a picked/sid face the axis must hit that face. An axis that hits no such face is refused by name. Never send both `positions` and `world_points`.

Example 0 (simplest) - one ⌀5 through-hole in the centre of the top face:
[
 {"op":"add_feature","feature":{"type":"hole","name":"Bore","params":{"style":"simple","standard":"custom","diameter":{"value":5,"expr":null},"end":"through_all"},"refs":{"face":{"of":"face","side":"top"}}}}
]

For a RING of holes (bolt circle) use the `bolt_circle` feature, not many `hole` ops — see ops cheatsheet.

Example A - M3 through-hole on box f1's top face, points from sketch f2:
[
 {"op":"add_feature","feature":{"type":"hole","name":"Hole1","params":{"style":"simple","standard":"M3","end":"through_all"},"refs":{"face":{"kind":"face","surf":"plane","centroid":[0,0,5],"axis":[0,0,1],"area_or_len":2400,"owner_feature":"f1","hint_index":0},"positions_sketch":"f2"}}}
]

Example B - custom 5mm diameter blind hole 8mm deep:
[
 {"op":"add_feature","feature":{"type":"hole","name":"Bore","params":{"style":"simple","standard":"custom","diameter":{"value":5,"expr":null},"end":"blind","depth":{"value":8,"expr":null}},"refs":{"face":{"kind":"face","surf":"plane","centroid":[0,0,5],"axis":[0,0,1],"area_or_len":2400,"owner_feature":"f1","hint_index":0},"positions_sketch":"f2"}}}
]

3 common mistakes:
1. You do NOT need a points sketch — pass params.positions:[[u,v],..] (or omit for a centre hole). Use positions_sketch only when the points already come from a real sketch.
2. Standard "M3" sets the diameter automatically; only set `diameter` when standard is "custom".
3. For a simple round bore without standards, a `cylinder` with mode "cut" is often simpler than `hole`.
