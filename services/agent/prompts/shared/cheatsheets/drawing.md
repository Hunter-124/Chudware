DRAWING — a controlled 2D manufacturing document for the CURRENT part or assembly. It is not the 3D sketcher, and a clean-looking page is not proof that the design specification is on-sheet.

AUTHORING ORDER
1. For a manufacturing release, read the ledger/specification and create a coverage plan: critical annotation rows need `{view_id, annotation_id, value, tolerance, source}`.
2. `make_drawing` with `model_items:false` for shop drawings. Native model-items are envelope/circle hints, not a manufacturing dimension scheme.
3. Add the views that expose each feature, then semantic dimensions/callouts and controlled notes.
4. `review_drawing` after the last mutation for pixels/layout. A requested draft may be exported without a release audit; label it as a draft, not certified.
5. For release, `audit_drawing` against real requirements. Export as release only when `verdict:"release_ready"` and `missing_critical` is empty. Never invent requirements or use an empty spec.

STARTING SHEET
```json
{"tool":"make_drawing","args":{
  "doc_id":"<source_part_or_assembly_doc_id>",
  "name":"BRACKET DRAWING",
  "sheet":"A3",
  "views":["front","top","right","iso"],
  "model_items":false,
  "hole_callouts":false,
  "scale":1.0,
  "title":{"title":"BRACKET"}
}}
```
`make_drawing` returns `drawing_doc_id` and a view map. `scale` controls geometric view scale and sets matching title text; do not set only the printed scale. Coordinates for annotations, cut lines and positions are sheet millimetres from `review_drawing`; never use model coordinates or eyeballed pixels.

VIEWS
- `add_drawing_view` adds base/projected views and supports hidden-line visibility/orientation controls.
- `set_view` moves/scales a view and can change hidden-line/orientation state.
- `add_section_view` supports centre-axis cuts plus explicit plane/offset/depth and multipoint cut lines; use sections for blind bores, threads, internal steps and weld details.
- `add_detail_view` magnifies a local region. Load lower-frequency drawing siblings with `enable_tools(categories=["drawing"])`.

DIMENSIONS AND CALLOUTS
- `add_dimension` places linear/aligned/radius/diameter/angular/centre/note annotations. Supply tolerance data at creation (`tol_type` plus the live schema's upper/lower values); do not ship nominal-only functional sizes and do not hide tolerances in detached prose.
- `add_hole_callout` uses `a=[x,y]` in sheet millimetres at or near the projected circle or its centre, not a model SID. Specify simple/tapped/counterbore/countersink semantics, count, full thread designation where applicable, THRU or depth, and counterbore/countersink data as applicable; a projected tap-drill diameter is not a finished-thread callout.
- `add_chamfer_dimension` creates a real chamfer callout. `add_ordinate_dimensions` creates baseline/ordinate sets where chained dimensions would accumulate error.
- `edit_drawing_annotation` patches/deletes by `view_id` plus `annotation_id`. Use `tol_type="none"` to remove displayed tolerance; null patches are refused atomically. Re-review after any patch and re-audit before release.

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
- `export_drawing` resolves relative paths against the selected project root; absolute paths are accepted, but credential/key destinations are refused for output and companions.
- To persist a standalone editable drawing use `export_part(fmt="draw+", doc_id=<drawing_id>, path="sheet.draw+")`. `save_part` accepts the source part/assembly, not a drawing id; saving the open source `.step+` also saves its associated drawings.

RELEASE GATE
`review_drawing` answers “is the current sheet laid out cleanly?” It does not prove semantic completeness. Build `spec.requirements` from the ledger and call:
```json
{"tool":"audit_drawing","args":{
  "doc_id":"<source_part_or_assembly_doc_id>",
  "drawing_id":"<drawing_doc_id>",
  "spec":{"requirements":[
    {"requirement_id":"D1","type":"dimension_tolerance","view_id":"v1","annotation_id":"a7","critical":true},
    {"requirement_id":"T1","type":"thread","thread":"M6x1.0","critical":true},
    {"requirement_id":"F1","type":"finish","value":"0.8"},
    {"requirement_id":"G1","type":"gdt","symbol":"position"},
    {"requirement_id":"A1","type":"datum","label":"A"},
    {"requirement_id":"W1","type":"weld","text":"fillet"},
    {"requirement_id":"TB1","type":"title_field","field":"finish","expected":"HARDEN 55-58 HRC"}
  ]}
}}
```
For release, an empty spec is refused: obtain the actual manufacturing requirements rather than fabricating them. Pair `view_id` with `annotation_id`, since IDs can repeat across views. Resolve every `coverage_matrix` row and every `missing_critical` item. `layout_clean:true`, a legacy `manufacturing_ready` field, or a separate Markdown specification never substitutes for a current `verdict:"release_ready"`. Draft export does not imply release certification.
