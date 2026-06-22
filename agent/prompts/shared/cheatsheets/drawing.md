DRAWING - a 2D engineering drawing (projected views + dimensions + title block) of the CURRENT part. This is the drafting sheet, NOT the 3D sketcher.

When the user asks for a "drawing", "blueprint", "engineering drawing", "dimensioned views", "make a drawing of this", or a DXF/PDF of the part, author it in ONE shot with `make_drawing`. The engine projects the views (hidden-line removed), auto-dimensions, calls out holes, and fills the title block - you only supply a tiny spec.

ONE-SHOT (preferred - tiny, ideal for small/local models):
{"action":"make_drawing","spec":{
  "name":"<drawing name>",                       // optional
  "sheet":"A3",                                  // A4|A3|A2|A1|A0 or A|B|C|D|E (optional)
  "views":["front","top","right","iso"],         // any of front/back/top/bottom/left/right/iso
  "model_items":true,                            // auto-dimension every ortho view (default true)
  "hole_callouts":true,                          // add "n x (diameter)d" callouts to holes
  "title":{"title":"BRACKET","drawn_by":"AI-CAD","material":"AL 6061","scale":"1:1","rev":"A"}
}}

Example A - standard 3-view + iso, auto-dimensioned, holes called out, titled:
{"action":"make_drawing","spec":{"views":["front","top","right","iso"],"model_items":true,"hole_callouts":true,"title":{"title":"PLATE","material":"AL 6061","scale":"1:1","rev":"A"}}}

Example B - just a front + section, minimal:
{"action":"make_drawing","spec":{"views":["front"],"model_items":true,"title":{"title":"SHAFT"}}}

The one-shot `make_drawing` action maps to the worker RPC `drawing_make {source_doc_id, spec}` (source_doc_id = the current part's doc id). It returns {doc_id, drawing}.

GRANULAR ops (only when finer control is needed; each is a worker RPC on the drawing doc id returned by drawing_make):
- drawing_add_view {doc_id, type:"base|projected|iso", orientation, position:[x,y]}
- drawing_model_items {doc_id, view_id}                      -> auto-dimension one view
- drawing_add_callout {doc_id, view_id, kind:"hole", a:[x,y]} -> snap a circle, "(diameter)d" callout
- drawing_add_dim {doc_id, view_id, type:"linear_dim|aligned_dim|radial_dim|diameter_dim|angular_dim", a:[x,y], b:[x,y], pos:[x,y]}
- drawing_set_annotation {doc_id, view_id, ann_id, tol_type:"symmetric|deviation|limit|basic", tol_upper, tol_lower, prefix, suffix, precision, critical:true}
- drawing_set_titleblock {doc_id, sheet_id:"s1", fields:{...}}
- export_drawing {doc_id, path, format:"dxf|pdf"}

3 common mistakes:
1. A drawing projects the CURRENT part - build/finish the 3D model first, THEN make_drawing.
2. Coordinates a/b/pos are SHEET millimetres (the projected page), not model coordinates.
3. Mark a critical dimension with drawing_set_annotation {critical:true, tol_type:"symmetric", tol_upper:0.1} - do NOT bake the tolerance into the dim text.
