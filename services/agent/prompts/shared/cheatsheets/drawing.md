DRAWING - a 2D engineering drawing (projected views + dimensions + GD&T + title block) of the CURRENT part. This is the drafting sheet, NOT the 3D sketcher.

When the user asks for a "drawing", "blueprint", "engineering drawing", "dimensioned views", "GD&T", "make a drawing of this", or a DXF/PDF of the part, author it in ONE shot with `make_drawing`, then REFINE it with the granular ops. The engine projects the views (hidden-line removed), auto-dimensions, calls out holes, cuts sections, and fills the title block.

ONE-SHOT (preferred starting point - tiny, ideal for small/local models):
{"action":"make_drawing","spec":{
  "name":"<drawing name>",                       // optional
  "sheet":"A3",                                  // A4|A3|A2|A1|A0 or A|B|C|D|E (optional)
  "views":["front","top","right","iso"],         // any of front/back/top/bottom/left/right/iso
  "model_items":true,                            // auto-dimension every ortho view (default true)
  "hole_callouts":true,                          // add "n x (diameter)d" callouts to holes
  "sections":[{"parent":"front","axis":"vertical","mode":"half"}],  // optional auto-sections
  "title":{"title":"BRACKET","drawn_by":"Chudware","material":"AL 6061","scale":"1:1","rev":"A"}
}}

`sections` cut a parent view through its centre - NO sheet-mm math needed. Each: {"parent":"front"|view_id, "axis":"vertical"|"horizontal", "mode":"full"|"half", "reverse":false, "label":"A"}.

`make_drawing` maps to the worker RPC `drawing_make {source_doc_id, spec}` and returns {doc_id, drawing}.

THE REVIEW LOOP (this is how you get a PROFESSIONAL sheet): after make_drawing,
  1. render the drawing to an image and LOOK at it (review_drawing / render_part on the drawing doc id),
  2. judge it: missing/overlapping dimensions? views colliding? GD&T placed off the feature? empty title block? wrong scale?
  3. fix with the granular ops below (read coordinates off the rendered sheet - they are SHEET millimetres),
  4. render again. Repeat until it reads like a real drawing.

GRANULAR ops (worker RPCs on the drawing doc id returned by drawing_make):
- drawing_add_view {doc_id, type:"base|projected|iso", orientation, position:[x,y]}
- drawing_set_view {doc_id, view_id, position:[x,y], scale}   -> MOVE / RESCALE a view (spread overlaps; scale 1.0=1:1, 0.5=1:2, 2.0=2:1)
- drawing_remove_view {doc_id, view_id}
- drawing_add_section {doc_id, parent_view_id, cut_line:[[x,y],[x,y],...], opts:{mode:"full|half", reverse:false}}  -- cut_line may be multi-point (offset/jogged section)
- drawing_add_detail {doc_id, parent_view_id, center:[x,y], radius}
- drawing_model_items {doc_id, view_id}                      -> auto-dimension one view
- drawing_add_callout {doc_id, view_id, kind:"hole", a:[x,y]} -> snap a circle, "(diameter)d" callout
- drawing_add_dim {doc_id, view_id, type:"linear_dim|aligned_dim|radial_dim|diameter_dim|angular_dim", a:[x,y], b:[x,y], pos:[x,y]}
- drawing_set_annotation {doc_id, view_id, ann_id, tol_type:"symmetric|deviation|limit|basic", tol_upper, tol_lower, prefix, suffix, precision, critical:true}
- drawing_set_titleblock {doc_id, sheet_id:"s1", fields:{...}}
- export_drawing {doc_id, path, format:"dxf|pdf"}

GD&T (ASME Y14.5) - vector symbols rendered identically on screen, in the PNG, and in the DXF/PDF:
- drawing_add_gdt {doc_id, view_id, spec:{symbol, tol_value, tol_diameter:bool, tol_modifier:"none|mmc|lmc|projected", datums:[{ref:"A",modifier:"none|mmc|lmc"}], a:[x,y] (feature point), pos:[x,y] (frame location)}}
    symbol = position|flatness|perpendicularity|parallelism|angularity|concentricity|symmetry|profile_surface|profile_line|circularity|cylindricity|straightness|circular_runout|total_runout
- drawing_add_datum {doc_id, view_id, spec:{label:"A", a:[x,y], pos:[x,y]}}                 -> datum feature (triangle + boxed letter)
- drawing_add_datum_target {doc_id, view_id, spec:{label:"A1", target_type:"point|line|area", size, a, pos}}
- drawing_add_surface_finish {doc_id, view_id, spec:{value:"1.6", machining:"any|required|prohibited", all_around:bool, a, pos}}
- drawing_add_weld {doc_id, view_id, spec:{weld_type:"fillet|square|vee|bevel|plug", size:"6", other_side:bool, all_around:bool, a, pos}}

Example - 3-view + iso, auto-dimensioned, a half section, a position callout to datum A:
{"action":"make_drawing","spec":{"views":["front","top","right","iso"],"model_items":true,"hole_callouts":true,"sections":[{"parent":"top","axis":"horizontal","mode":"half"}],"title":{"title":"PLATE","material":"AL 6061","scale":"1:1","rev":"A"}}}
(then) drawing_add_datum {view_id, spec:{label:"A", a:[..], pos:[..]}}
(then) drawing_add_gdt {view_id, spec:{symbol:"position", tol_value:"0.1", tol_diameter:true, tol_modifier:"mmc", datums:[{ref:"A"}], a:[..], pos:[..]}}

4 common mistakes:
1. A drawing projects the CURRENT part - build/finish the 3D model first, THEN make_drawing.
2. Coordinates a/b/pos/center/cut_line are SHEET millimetres (the projected page) - read them off review_drawing, do NOT use model coordinates.
3. Mark a critical size with drawing_set_annotation {critical:true, tol_type:"symmetric", tol_upper:0.1} - do NOT bake the tolerance into the dim text. For geometric control (position/flatness/...) use drawing_add_gdt, not a note.
4. Don't ship the first pass - ALWAYS review_drawing and fix overlaps / missing dims before export_drawing.
