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

GRANULAR TOOLS (use the drawing `doc_id` returned by `make_drawing`):
- `add_drawing_view {doc_id, orientation, view_type, x, y}`
- `set_view {doc_id, view_id, x?, y?, scale?}` — move or rescale a view.
- `add_section_view {doc_id, parent, axis, mode, reverse?, label?}`
- `add_detail_view {doc_id, parent_view_id, cx, cy, radius}`
- `add_dimension {doc_id, view_id, dtype, a, b?, pos?}` — `a`/`b` may be sheet points or model feature ids.
- `set_titleblock {doc_id, fields:{title, drawn_by, date, material, finish, scale, rev, sheet, part_number, company}}`
- `export_drawing {doc_id, path, fmt:"dxf|pdf"}`

GD&T (ASME Y14.5):
- `add_gdt {doc_id, view_id, symbol, tolerance, a, pos, datums?, diameter?, modifier?}`
  where `symbol` is position|flatness|perpendicularity|parallelism|angularity|concentricity|
  symmetry|profile_surface|profile_line|circularity|cylindricity|straightness|
  circular_runout|total_runout.
- `add_datum {doc_id, view_id, label, a, pos}`
- `add_surface_finish {doc_id, view_id, ra, a, pos, machining?, all_around?}`

Example - 3-view + iso, auto-dimensioned, a half section, a position callout to datum A:
`make_drawing {views:["front","top","right","iso"], model_items:true, hole_callouts:true, sections:[{parent:"top",axis:"horizontal",mode:"half"}], title:{title:"PLATE",material:"AL 6061",scale:"1:1",rev:"A"}}`
(then) `add_datum {view_id, label:"A", a:[..], pos:[..]}`
(then) `add_gdt {view_id, symbol:"position", tolerance:"0.1", diameter:true, modifier:"mmc", datums:["A"], a:[..], pos:[..]}`

4 common mistakes:
1. A drawing projects the CURRENT part - build/finish the 3D model first, THEN make_drawing.
2. Coordinates a/b/pos/center/cut_line are SHEET millimetres (the projected page) - read them off review_drawing, do NOT use model coordinates.
3. Use `add_gdt` for geometric controls; do not invent unsupported annotation-edit or weld-symbol tools.
4. Don't ship the first pass - ALWAYS review_drawing and fix overlaps / missing dims before export_drawing.
