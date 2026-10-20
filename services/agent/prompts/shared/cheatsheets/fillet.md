FILLET - round off edges with a radius. params: `radius` (dim object). refs.edges names the edges.

BEST WAY (works on ANY part, including ones you did not build) - a geometry QUERY under refs.edges. You NAME the edges in PLAIN WORDS instead of computing selectors or face numbers:
- {"of":"edge"}                       every edge of the body (round the whole part)
- {"of":"edge","parallel":"z"}        the vertical edges only (the 4 corners of a block); "x"/"y" too
- {"of":"edge","side":"top"}          the TOP edges / top rim   (bottom/left/right/front/back too)
- {"of":"edge","surf":"round"}        the round edges (a cylinder's rims)
- {"of":"edge","pick":"longest"}      the longest edge(s) ("shortest" too)
- {"of":"edge","near":[20,0,5],"count":1}  the single edge nearest a point
Direction words map to the standard views: top=+Z, bottom=-Z, right=+X, left=-X, front=-Y, back=+Y
(the signed-axis forms "+z"/"-x"/.. still work if you prefer them).

Example A - round ALL edges of the current body r=2:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Fillet1","params":{"radius":{"value":2,"expr":null}},"refs":{"edges":{"of":"edge"}}}}
]

Example B - round just the 4 vertical edges of a block r=5:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Round","params":{"radius":{"value":5,"expr":null}},"refs":{"edges":{"of":"edge","parallel":"z"}}}}
]

Example C - round the top rim of a round flange r=1:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Rim","params":{"radius":{"value":1,"expr":null}},"refs":{"edges":{"of":"edge","side":"top"}}}}
]

VARIABLE-RADIUS fillet: add `radius2` param — the fillet tapers linearly from `radius` at the
start of each edge to `radius2` at the end. Requires explicit edge picks (not a plain direction
query). Both radius and radius2 must be > 0.
Example D - taper edge from 3mm to 1mm (variable):
[
 {"op":"add_feature","feature":{"type":"fillet","name":"TaperFillet","params":{"radius":{"value":3,"expr":null},"radius2":{"value":1,"expr":null}},"refs":{"edges":{"of":"edge","side":"top"}}}}
]

3 common mistakes:
1. The query goes under refs.edges (an OBJECT with "of"), not at the feature top level.
2. Radius must be SMALLER than the thinnest wall it touches or the build fails (auto-clamp may shrink it).
3. To discover what edges exist on a part you did not build, call the `doc_query` tool with {"of":"edge",...} first - it returns each edge's surf/centroid/axis.

The final solid must pass native B-rep, zero-area-face and self-intersection checks
before it replaces the preceding body. A kernel build alone is not success.
An invalid result rejects the edit atomically with its native reason; inspect the
selected edges and adjacent material before changing the radius or construction.
Do not repeat the mutation after a result-reporting exception without reading its
recorded reply and current tree.
