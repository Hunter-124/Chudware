FILLET - round off edges with a radius. params: `radius` (dim object). refs.edges names the edges.

BEST WAY (works on ANY part, including ones you did not build) - a geometry QUERY under refs.edges. You NAME the edges instead of computing selectors:
- {"of":"edge"}                  every edge of the body (round the whole part)
- {"of":"edge","parallel":[0,0,1]} the vertical edges only (e.g. the 4 corners of a block)
- {"of":"edge","side":"+z"}      the TOP edges / top rim   ("-z" = bottom; "+x"/"-x"/"+y"/"-y" too)
- {"of":"edge","surf":"circle"}  the round edges (a cylinder's rims)
- {"of":"edge","near":[20,0,5],"count":1}  the single edge nearest a point

Example A - round ALL edges of the current body r=2:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Fillet1","params":{"radius":{"value":2,"expr":null}},"refs":{"edges":{"of":"edge"}}}}
]

Example B - round just the 4 vertical edges of a block r=5:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Round","params":{"radius":{"value":5,"expr":null}},"refs":{"edges":{"of":"edge","parallel":[0,0,1]}}}}
]

Example C - round the top rim of a round flange r=1:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Rim","params":{"radius":{"value":1,"expr":null}},"refs":{"edges":{"of":"edge","side":"+z"}}}}
]

3 common mistakes:
1. The query goes under refs.edges (an OBJECT with "of"), not at the feature top level.
2. Radius must be SMALLER than the thinnest wall it touches or the build fails (auto-clamp may shrink it).
3. To discover what edges exist on a part you did not build, call the `doc_query` tool with {"of":"edge",...} first - it returns each edge's surf/centroid/axis.
