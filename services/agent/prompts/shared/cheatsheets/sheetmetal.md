SHEET METAL (sm_*) — model a constant-thickness folded-metal part the right way: a flat BASE
FLANGE, then walls bent up off its straight edges, folded hems, and Z-jogs, and finally the
developed FLAT BLANK. Every sm_* op yields a watertight solid. This is the RIGHT way to model a
bracket, enclosure, chassis panel, or tray — do NOT approximate bends with stacked boxes.

FEATURE TYPES (params shown with their defaults; required ref in []):
  sm_base_flange   params: thickness (REQUIRED), k_factor (0.45), default_bend_radius (=thickness)
                   refs.profile = [region] — a CLOSED sketch region. Extrudes it to `thickness`
                   AND marks the part as sheet metal (later bends inherit k_factor /
                   default_bend_radius). Do this first; everything else bends off it.
  sm_edge_flange   params: length (20), angle (90, deg), radius (=default_bend_radius),
                   position ("material_inside" | "material_outside"), k_factor, flip (false)
                   refs.edge = the bend line. Adds a real bent wall (bend + flat) of `length`.
  sm_hem           params: length (=5*t, clamped to >=1.5*t), hem_type ("closed"|"open"|"teardrop"),
                   radius, k_factor, flip ; refs.edge. A 180° (235° teardrop) folded-back hem that
                   stiffens an edge / removes the sharp.
  sm_jog           params: length (20), offset (10), angle (<=90), radius, position, k_factor, flip
                   refs.edge. A Z-shaped two-bend step that offsets the wall by `offset`.
  sm_flat_pattern  params/refs: none. REPLACES the running body with its developed flat blank
                   (volume changes a few % — that IS the bend allowance, not an error). Export the
                   folded part BEFORE this op if you want to keep both the part and the blank.

INVOCATION (these features predate the cheatsheets — follow exactly):
  - One doc_apply_ops batch: refer to an earlier op as "$N" (its 0-based index). A SEPARATE batch
    refers to a created feature by its assigned id "fN". Never invent ids.
  - profile ref = [{"kind":"region","sketch":"$0","centroid":[x,y],"hint_index":0}] (a LIST).
  - THE BEND EDGE must be a STRAIGHT edge lying on a THICKNESS face (a face ~thickness wide) — i.e.
    an outline edge at the sheet's top rim (z ~= thickness). You CANNOT drop the {"of":"edge",...}
    query shorthand straight into refs.edge: this slot resolves the ref directly and rejects a bare
    query as "not an edge selector" (unlike fillet/chamfer, which resolve a query internally). So
    run query_geometry / doc_query FIRST and pass the RESOLVED match object back as refs.edge. Use
    near+count:1 so the query returns exactly ONE edge:
      query_geometry({"of":"edge","surf":"line","near":[x,y,z],"count":1})
        -> a match like {"kind":"edge","centroid":[x,y,z],"axis":[..],"surf":"line",...}

WORKED EXAMPLE — an L-bracket: an 80x50x2 base flange + one 20mm wall bent up 90° on the +X edge.
This takes TWO batches because the bend edge has to be queried off the just-built base flange.

PHASE 1 — sketch the 80x50 rectangle and make the base flange (one doc_apply_ops batch):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Base","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"rectangle","center":[0,0],"width":80,"height":50}},
 {"op":"add_feature","feature":{"type":"sm_base_flange","name":"Flange","params":{"thickness":{"value":2,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[0,0],"hint_index":0}]}}}
]
 (4 separate line entities for the same outline remain valid; rectangle is preferred.)

PHASE 2 — find the +X top-rim edge, then bend a 20mm wall up 90°. The top face sits at z=2, so the
+X rim edge's midpoint is [40,0,2]. query_geometry returns the RESOLVED selector; pass it as refs.edge:
  query_geometry({"of":"edge","surf":"line","near":[40,0,2],"count":1})
    -> {"kind":"edge","centroid":[40,0,2],"axis":[0,1,0],"surf":"line","area_or_len":50,"hint_index":0}
[
 {"op":"add_feature","feature":{"type":"sm_edge_flange","name":"Wall","params":{"length":{"value":20,"expr":null},"angle":{"value":90,"expr":null}},"refs":{"edge":{"kind":"edge","centroid":[40,0,2],"axis":[0,1,0],"surf":"line","area_or_len":50,"hint_index":0}}}}
]
(Append an sm_flat_pattern op {"op":"add_feature","feature":{"type":"sm_flat_pattern","name":"Flat","params":{}}} to unfold the blank.)

3 common mistakes:
1. sm_base_flange must come FIRST — every other sm_* op errors with "part is not sheet metal" until
   a base flange marks the body. profile must be ONE closed sketch region.
2. The bend `edge` ref must be a RESOLVED edge selector (an object WITH a centroid, from a query) —
   the bare {"of":"edge",...} query shorthand is rejected here ("not an edge selector"). Pin exactly
   one straight edge on a thickness face (the top rim) with near+count:1 — don't catch the bottom rim.
3. thickness must be small vs the part, and `length`/`radius` must be > 0 — degenerate values error
   loudly (no silent-wrong geometry). render_part() after bends to confirm the walls landed right.

LIMIT (by design, not a bug): a 3rd/4th flange on ADJACENT edges of the same base can fail — once
the first walls land, the shared corner edge no longer spans the full side, and real sheet metal
needs corner relief the builder does not auto-generate. A 1-2-flange channel is the clean limit.

Confirmed against core/doc/validate.cpp (FEATURE_TYPES sm_*), core/doc/document.cpp handlers, and
core/geom/sheetmetal.cpp; live driver pass: base_flange 80x50x2 = 8000mm³ exact, edge_flange/hem/
jog watertight, flat_pattern unfolds with the correct bend allowance.
