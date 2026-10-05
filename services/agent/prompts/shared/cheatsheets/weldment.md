WELDMENTS (weld_*) — build a structural frame by sweeping a stock profile along the segments of a
path sketch: ONE member body per segment, with mitred / butt / coped corners, plus gussets and end
caps. This is the RIGHT way to model a tube / angle / channel / beam frame (chassis, ladder, rack,
railing) — do NOT model each member as a hand-placed box.

weld_member — params:
  profile (REQUIRED) — the stock cross-section, ONE of (each dim shown, all have defaults):
     square_tube {size,thickness}    rect_tube {w,h,thickness}    round_tube {od,thickness}
     angle_L {leg,thickness}         flat_bar {w,thickness}       c_channel {h,w,thickness}
     i_beam {h,w,tw,tf}              t_section {h,w,tw,tf}
  justify ("center"|"corner"), angle (0 — roll the section about the path axis, deg),
  trim_corners (true), joint ("miter"|"butt"|"cope") — how shared corners are trimmed.
  refs.sketch = the PATH SKETCH id as a BARE STRING ("$0" or "fN"). The path's OPEN line/arc
  segments ARE the members; a closed circle or a construction entity is skipped.
  Optional params.segments = [entity ids] to use a subset (default = every segment).
  -> ONE SEPARATE BODY per segment; straight members are trimmed to each other at shared corners
     per `joint`.

weld_gusset — params: thickness (5), d1 (30), d2 (30), type ("triangular"|"polygonal"),
  location ("centered"|"inner"|"outer"), offset (0) ; refs.faces = EXACTLY two planar member faces
  at the corner. A stiffener plate spanning the two members.
weld_end_cap — params: thickness (5), type ("outer"|"inner"), offset (0), overhang (0; >0 is
  rejected) ; refs.face = ONE planar member-end face. An end plate closing a tube end.

INVOCATION:
  - One batch: "$N" = an earlier op's 0-based index; a separate batch uses the assigned "fN".
  - Per-profile dims go in params (a bare number OR a {"value":n,"expr":null} dim both work).
  - ** refs.sketch is a BARE STRING id, NOT a {"kind":"region",...} selector. ** This one slot
    breaks the usual selector convention (a known inconsistency): a selector object errors with
    "missing string ref 'sketch'". Pass the path sketch's id directly ("$0").
  - gusset / end_cap face refs need a CONCRETE resolved face selector (with centroid + owner_feature)
    from a query — the {"of":"face",...} shorthand and a region selector won't do. KNOWN LIMIT:
    query_geometry currently enumerates faces of only ONE member body at a time, so a face on the
    second member is hard to address. Build the frame first; add gussets/caps only where you can pin
    the face. (weld_member + the three joints below are the fully-reachable capability.)

WORKED EXAMPLE — a 300x180 rectangular frame in 25mm square tube, mitred corners (4 path lines ->
4 members). Note refs.sketch is the BARE STRING "$0":
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Path","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[-150,-90],"p2":[150,-90]}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[150,-90],"p2":[150,90]}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[150,90],"p2":[-150,90]}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[-150,90],"p2":[-150,-90]}},
 {"op":"add_feature","feature":{"type":"weld_member","name":"Frame","params":{"profile":"square_tube","size":{"value":25,"expr":null},"thickness":{"value":2,"expr":null},"joint":"miter"},"refs":{"sketch":"$0"}}}
]
-> four mitred 25mm square-tube members, one body each. render_part() to confirm the corners.

3 common mistakes:
1. refs.sketch must be a BARE STRING id, not a region selector (the one slot that breaks the
   selector convention).
2. The path's segments ARE the members — give OPEN lines/arcs; a closed circle yields no members
   ("sketch has no usable segments to sweep along").
3. The tube wall `thickness` must be < size/2 (or w/2, od/2, ...), or the section is degenerate and
   the op errors ("degenerate profile dimensions").
