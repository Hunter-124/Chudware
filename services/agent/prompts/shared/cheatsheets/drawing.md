DRAWING — a controlled 2D manufacturing document for the CURRENT part or assembly. It is not the 3D sketcher, and a clean-looking page is not proof that the design specification is on-sheet.

AUTHORING ORDER
1. Read the ledger/specification and create a coverage plan: every critical dimension/process row needs `{annotation_id, value, tolerance, source}`.
2. `make_drawing` with `model_items:false` for shop drawings. Native model-items are envelope/circle hints, not a manufacturing dimension scheme.
3. Add the views that expose each feature, then semantic dimensions/callouts and controlled notes.
4. `review_drawing` after the last mutation for pixels/layout.
5. `audit_drawing` against the specification. Export only when `verdict:"release_ready"` and `missing_critical` is empty.

STARTING SHEET
```json
{"action":"make_drawing","spec":{
  "name":"BRACKET DRAWING",
  "sheet":"A3",
  "views":["front","top","right","iso"],
  "model_items":false,
  "hole_callouts":false,
  "title":{"title":"BRACKET","drawn_by":"Chudware","date":"<YYYY-MM-DD>","material":"6061-T6","finish":"AS SPECIFIED","scale":"1:1","rev":"A","sheet":"1 OF 1","part_number":"BR-001","company":"ACME"}
}}
```
`make_drawing` returns the drawing `doc_id` and view map. Coordinates for annotations, cut lines and positions are sheet millimetres from `review_drawing`; never use model coordinates or eyeballed pixels.

VIEWS
- `add_drawing_view` adds base/projected views and supports hidden-line visibility/orientation controls.
- `set_view` moves/scales a view and can change hidden-line/orientation state.
- `add_section_view` supports centre-axis cuts plus explicit plane/offset/depth and multipoint cut lines; use sections for blind bores, threads, internal steps and weld details.
- `add_detail_view` magnifies a local region. Load lower-frequency drawing siblings with `enable_tools(categories=["drawing"])`.

DIMENSIONS AND CALLOUTS
- `add_dimension` places linear/aligned/radius/diameter/angular/centre/note annotations. Supply tolerance data at creation (`tol_type` plus the live schema's upper/lower values); do not ship nominal-only functional sizes and do not hide tolerances in detached prose.
- `add_hole_callout` is the semantic path for simple, tapped, counterbored and countersunk holes. Record count, full thread designation, THRU or depth, and counterbore/countersink data as applicable; a projected tap-drill diameter is not a finished-thread callout.
- `add_chamfer_dimension` creates a real chamfer callout. `add_ordinate_dimensions` creates baseline/ordinate sets where chained dimensions would accumulate error.
- `edit_drawing_annotation` patches/deletes an existing annotation by id. Re-review and re-audit after any patch.

DATUMS, GD&T, FINISH AND WELDS
- Declare `add_datum` before any frame that references it. Use `add_datum_target` for point/line/area targets.
- `add_gdt` supports the ASME Y14.5 characteristics, material/datum modifiers and a composite second row (`row2`). Build the frame from the ledger; never add decorative GD&T.
- `add_surface_finish` ties Ra/lay/machining requirements to the controlled surface.
- `add_weld_symbol` is a real structured tool: weld type/size/side plus intermittent length/pitch, contour/finish, all-around, field flag, tail text and full-penetration when required. Do not replace a weld symbol with a nearby note.

ASSEMBLIES, BOM AND MULTI-SHEET PACKAGES
- `drawing_bom` reads stable assembly item/name/quantity/source rows; `add_balloon` links item numbers to components/views.
- `add_drawing_table` places BOM, revision, holes, notes or cut-list tables. Use `add_drawing_sheet`, `list_drawing_sheets` and `set_active_drawing_sheet` for controlled multi-sheet packages.
- Assembly/weldment releases normally need an assembly drawing, balloons, BOM/cut list, required sections/weld symbols and controlled work/assembly instructions in addition to part drawings.
- `export_drawing(..., sheet:"all")` exports the complete controlled package when all sheets are required.

RELEASE GATE
`review_drawing` answers “is the current sheet laid out cleanly?” It does not prove semantic completeness. Build `spec.requirements` from the ledger and call:
```json
{"tool":"audit_drawing","args":{
  "doc_id":"<source_part_or_assembly_doc_id>",
  "drawing_id":"<drawing_doc_id>",
  "spec":{"requirements":[
    {"requirement_id":"D1","type":"dimension_tolerance","annotation_id":"a7","critical":true},
    {"requirement_id":"T1","type":"thread","thread":"M6x1.0","critical":true},
    {"requirement_id":"F1","type":"finish","value":"0.8"},
    {"requirement_id":"G1","type":"gdt","symbol":"position"},
    {"requirement_id":"A1","type":"datum","label":"A"},
    {"requirement_id":"W1","type":"weld","text":"fillet"},
    {"requirement_id":"TB1","type":"title_field","field":"finish","expected":"HARDEN 55-58 HRC"}
  ]}
}}
```
An empty spec is refused. Resolve every `coverage_matrix` row and every `missing_critical` item. `layout_clean:true`, a legacy `manufacturing_ready` field, or a separate Markdown specification never substitutes for `verdict:"release_ready"`. Call `export_drawing` only after that current verdict.
