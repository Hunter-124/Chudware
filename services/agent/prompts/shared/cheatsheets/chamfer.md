CHAMFER - bevel edges by a setback length. params: `length` (dim object - NOT radius). refs.edges names the edges.

BEST WAY (works on ANY part) - a geometry QUERY under refs.edges (same query language as fillet):
- {"of":"edge"}                  every edge
- {"of":"edge","side":"+z"}      the TOP edges / top rim   ("-z" = bottom; "+x"/"-x"/"+y"/"-y" too)
- {"of":"edge","parallel":[0,0,1]} the vertical edges
- {"of":"edge","surf":"circle"}  the round edges (cylinder rims)
- {"of":"edge","near":[x,y,z],"count":1}  the edge nearest a point

Example A - 1mm chamfer on the top rim of a round part:
[
 {"op":"add_feature","feature":{"type":"chamfer","name":"Rim","params":{"length":{"value":1,"expr":null}},"refs":{"edges":{"of":"edge","side":"+z"}}}}
]

Example B - 2mm chamfer on every edge:
[
 {"op":"add_feature","feature":{"type":"chamfer","name":"Chamfer1","params":{"length":{"value":2,"expr":null}},"refs":{"edges":{"of":"edge"}}}}
]

ASYMMETRIC chamfer: add `length2` param — leg d1=`length` on one incident face, d2=`length2` on
the other. Requires explicit edge picks (not a plain direction query). Useful for a different bevel
angle on each side of an edge.
Example C - asymmetric 2mm / 0.5mm chamfer on the top rim:
[
 {"op":"add_feature","feature":{"type":"chamfer","name":"AsymChamfer","params":{"length":{"value":2,"expr":null},"length2":{"value":0.5,"expr":null}},"refs":{"edges":{"of":"edge","side":"+z"}}}}
]

3 common mistakes:
1. Chamfer uses `length` (the setback), NOT `radius`. Using radius is rejected.
2. The query goes under refs.edges (an OBJECT with "of"), not at the feature top level.
3. Keep length smaller than the adjacent wall/face, or OCCT fails.

The final solid must pass native B-rep, zero-area-face and self-intersection checks
before it replaces the preceding body. Invalid geometry rejects atomically and
preserves the preceding body; the native error names the defect. Inspect adjacent
material and selected edges before revising length or construction.
