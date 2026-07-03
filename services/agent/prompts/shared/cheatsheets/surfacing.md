SURFACING (surf_*) — build zero-thickness SURFACE bodies (extrude / revolve / sweep / loft a
profile as an OPEN wall; ruled / fill / boundary patches; knit panels together), then turn them
into solids (THICKEN them, or replace a solid's face). This is the RIGHT way to model a blended
cover, a freeform shell, or a panel a solid feature can't make cleanly. A surface body has vol=0
(correct) until you thicken it or knit it watertight.

FEATURE TYPES (required params ; required refs):
  surf_planar     refs.profile                        -> flat surface from a closed sketch region.
  surf_extrude    params end, depth ; refs.profile    -> open side-walls (NO end caps). flip opt.
  surf_revolve    params angle ; refs.profile, axis   -> revolved wall. axis = a reserved datum.
  surf_sweep      refs.profile, path, [guides]        -> profile swept along a path (open tube).
  surf_loft       refs.sections (>=2), [guides,centerline] ; params ruled(bool) -> blended wall.
  surf_ruled      refs.edge_a, edge_b                 -> the exact ruled face between two edges.
  surf_offset     params distance ; refs.faces        -> offset a solid's face(s) into a surface.
  surf_fill       refs.edges (>=2) ; params continuity(g0/g1) -> a filled patch inside a boundary.
  surf_boundary   refs.dir1, [dir2]                   -> a boundary-curve patch (same filler).
  surf_knit       refs.surfaces (>=2)                 -> sew panels; PROMOTES to a SOLID if the
                  sewn result is watertight.
  surf_thicken    params thickness, side(one|both), flip ; refs.surface -> SURFACE -> SOLID. The
                  marquee surface-to-solid step: give the wall a thickness.
  surf_trim       params keep(positive|negative|both) ; refs.surface, tool -> split a surface by a
                  tool face, keep a side.
  surf_delete_face   refs.faces                       -> open a solid by removing face(s) -> surface.
  surf_replace_face  refs.faces, surface              -> swap a solid's face for a surface (BOP
                  heal); the replacement must FULLY SPAN the target face or you get an open shell.

INVOCATION:
  - One batch: "$N" = an earlier op's 0-based index; a separate batch uses the assigned "fN".
  - profile / section refs = [{"kind":"region","sketch":"$0","centroid":[x,y],"hint_index":0}].
  - a surface / tool / replacement BODY ref = ONE object {"kind":"body","owner_feature":"$1"} (the
    surface body produced by an earlier surf_* op). surf_knit surfaces = a LIST of these objects.
  - surf_revolve axis = {"kind":"datum","feature":"axis.z"} — only the reserved axis.x/y/z datums
    pass; kind:"axis" is rejected.
  - surf_ruled (edge_a/edge_b), surf_fill (edges), surf_boundary (dir1/dir2) each take EITHER a
    sketch/region ref (a whole solved sketch becomes the rail/boundary curve) OR a BODY-EDGE
    selector — the {"kind":"edge","owner_feature":"$N",...} object a viewport edge pick produces
    (the GUI ribbon wells emit these). A single picked edge becomes a one-edge rail; this is the
    SolidWorks-style "ruled/boundary surface between body edges" path. Edges resolve against the
    most-recent body, so keep the picked edges on the current body.
  - face refs (offset / delete_face / replace_face) need the RESOLVED face selector object from a
    query — NOT the {"of":"face",...} shorthand. Unlike shell/fillet (which resolve a query
    internally), these surf_* slots resolve refs.faces directly and reject a bare query as
    "dangling". Run query_geometry({"of":"face",...}) and pass back the returned match(es), e.g.
    {"kind":"face","centroid":[x,y,z],"surf":"plane",...}.

WORKED EXAMPLE — a thin tapered shroud: loft an 80x60 rectangle up to a 50x40 rectangle (an open
surface wall), then thicken it into a 2.5mm constant-wall solid:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Bottom","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[-40,-30],"p2":[40,-30]}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[40,-30],"p2":[40,30]}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[40,30],"p2":[-40,30]}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[-40,30],"p2":[-40,-30]}},
 {"op":"add_feature","feature":{"type":"sketch","name":"Top","params":{"entities":[],"constraints":[],"offset":{"value":40,"expr":null}},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$5","entity":{"type":"line","p1":[-25,-20],"p2":[25,-20]}},
 {"op":"sketch_add_entity","sketch":"$5","entity":{"type":"line","p1":[25,-20],"p2":[25,20]}},
 {"op":"sketch_add_entity","sketch":"$5","entity":{"type":"line","p1":[25,20],"p2":[-25,20]}},
 {"op":"sketch_add_entity","sketch":"$5","entity":{"type":"line","p1":[-25,20],"p2":[-25,-20]}},
 {"op":"add_feature","feature":{"type":"surf_loft","name":"Wall","params":{},"refs":{"sections":[{"kind":"region","sketch":"$0","centroid":[0,0],"hint_index":0},{"kind":"region","sketch":"$5","centroid":[0,0],"hint_index":0}]}}},
 {"op":"add_feature","feature":{"type":"surf_thicken","name":"Shell","params":{"thickness":{"value":2.5,"expr":null},"side":"one"},"refs":{"surface":{"kind":"body","owner_feature":"$10"}}}}
]
-> a watertight tapered shroud. render_part() + measure_part() to confirm the wall.

3 common mistakes:
1. A surface body is vol=0 by design — it is NOT a solid until you surf_thicken it or surf_knit a
   closed set of panels. Don't run FEA / mass on a bare surface.
2. The surface/tool/replacement ref is a BODY object {"kind":"body","owner_feature":"$N"}, while
   profile/section refs are [region] lists — don't mix them up.
3. surf_revolve only accepts a reserved axis datum {"kind":"datum","feature":"axis.z"} (not a sketch
   line, not kind:"axis").

KNOWN LIMITS:
1. surf_thicken can FAIL on a TOPOLOGY-CHANGING loft (e.g. a rectangle blended to a CIRCLE — the
   section's edge count changes) — OCCT's offset is fragile there. It works on planar / ruled /
   rect->rect lofts. If a thicken fails, keep matching section topology, or build the wall into the
   profile (loft a thin closed ring) instead.
2. (none currently) — surf_untrim now has a working handler (creates an untrimmed patch as a new
   body, source surface intact); the old "no compiler for surf_untrim" note is obsolete.

Confirmed against core/doc/validate.cpp (FEATURE_TYPES surf_*), core/doc/document.cpp handlers, and
core/geom/surfacing.cpp; live driver pass: planar/extrude/revolve/sweep/loft/ruled/offset/fill/
boundary/knit/thicken/trim/untrim/delete_face/replace_face all build (surf_thicken loft caveat
above). ruled/fill/boundary accept body-edge picks as of T-BL28 (regression: core/tests/
test_surfacing.cpp).
