# Guide: Drawings

```text
2D ENGINEERING DRAWINGS — professional sheets from the live part or assembly

WHAT A NEW DRAWING CONTAINS (Make Drawing in the app, make_drawing for agents)
  - Paper: ISO A4-A0 or ANSI A-E, landscape by default; "A4 portrait" / "A4P" for portrait.
  - ISO 5457 frame: 20 mm filing margin, 10 mm elsewhere, zone letters/numbers in the margin,
    centring marks.
  - ISO 7200 title block (180 mm wide, lower right): TITLE, DRAWING / PART NO., REV, SHEET
    (auto "n OF m"), MATERIAL (from the part), FINISH, MASS, SCALE, SIZE, DRAWN, DATE (today),
    CHECKED, APPROVED, COMPANY, the ISO 5456-2 projection symbol, UNITS (the drawing's unit
    system — change with drawing units, never by typing), GENERAL TOLERANCES and the standard.
    Values shrink to fit their cell and never overflow it.
  - Views: front / top / right + an isometric, on the projection lattice — third angle (ASME,
    default: top above front, right view to its right) or first angle (ISO E: mirrored). Views
    share projection lines, are drawn at ONE standard ISO 5455 scale chosen to fit the free
    area (frame minus title block and tables), and never cover the title block.
  - Line types per ISO 128: visible outlines 0.5 mm continuous, hidden edges 0.25 mm dashed,
    centre lines 0.25 mm long-dash-dot, dimensions/hatching 0.25 mm. The isometric carries no
    hidden lines.
  - Auto-dimensions per ISO 129-1: each overall extent stated once on the sheet (no redundant
    dims), round outlines by their diameter, one "N× ⌀d" callout per distinct hole size, centre
    marks on every hole, centre lines on hole/boss axes seen side-on, and a chain pitch circle
    with its diameter for equally spaced hole patterns. Filled 3 mm arrowheads, 3.5 mm text,
    extension lines with a 1 mm gap and 2 mm overshoot, arrows flip outside on tight spans.
  - Assemblies: overall envelope, a parts list (BOM) sitting on the title block, and one balloon
    per BOM item.

EDITING
  - Sections: chain cutting plane with thick ends, viewing arrows and letters on the parent,
    hatched cut faces, "SECTION A-A" caption. Details: boundary circle + letter on the parent,
    "DETAIL B  SCALE 2:1" caption.
  - arrange_drawing(doc_id, projection?, scale?, size?) re-lays a sheet (switch first/third
    angle, force a standard scale, change paper) and moves dimensions and cuts with their views.
  - Dimensions, tolerances (symmetric / deviation / limit / basic), GD&T frames, datums, surface
    finish, weld symbols, notes, balloons, tables: see the agent cheatsheet below.

DELIVERABLES (export_drawing)
  - PDF: vector, true paper size, ISO line weights, filled arrows, searchable Helvetica text.
  - DXF R12: layers VISIBLE, HIDDEN (DASHED), CENTER, DIMENSIONS, HATCH, BORDER, TABLES; opens in
    any CAD package.
  - DWG (AutoCAD R2000, written natively): the same layers/colors/text, DASHED/CENTER
    linetypes and aligned text.
  - SVG: editable paths and real text. PNG review raster via review_drawing.
  - A non-release draft may be exported after review without audit; identify it as a draft.
    For release, audit_drawing against real manufacturing requirements; empty specs refuse.
    The audit also derives bore and feature dimensions from the live model, checks every sheet
    and flags stale anchors and text overrides that disagree with measured values.
    Unevaluated assembly component schemes and equation-driven feature nominals are reported
    as missing critical requirements, not release evidence.
    Pair view_id with annotation_id, since IDs can repeat between views.
  - Set geometric scale with make_drawing/arrange_drawing, not only printed title text.
    review_drawing and audit_drawing report scale disagreements.
  - add_hole_callout takes a=[x,y] sheet mm near a projected circle or its centre,
    not model SIDs. Thread, depth and counterbore/countersink details require a matching
    Hole feature_ref. To clear displayed tolerance use tol_type="none"; null patches refuse.
    Dimension planners require live authored geometry and never substitute fixture data.
  - Relative export_drawing paths use the selected project root; absolute paths are accepted,
    except credential/key destinations, including companions.
  - Drawings save inside the open source part's .step+ via save_part. For a standalone native
    .draw+ embedding its source, use export_part(fmt="draw+", doc_id=<drawing_id>,
    path="sheet.draw+"); save_part does not accept drawing ids.

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
- `add_dimension` places linear/aligned/radius/diameter/angular/centre/note annotations. Angular dimensions use four sheet points or two visible straight edge SIDs; linear/aligned dimensions accept one edge SID or two parallel planar face SIDs when the view exposes their true separation. Dimension anchors that cannot be re-resolved are marked stale. Supply tolerance data at creation (`tol_type` plus the live schema's upper/lower values); do not ship nominal-only functional sizes and do not hide tolerances in detached prose.
- `add_hole_callout` uses `a=[x,y]` in sheet millimetres at or near the projected circle or its centre, not a model SID. Provide a matching Hole `feature_ref` for thread, THRU/depth, counterbore, countersink and verified count data; the diameter must agree with the selected model circle.
- `add_chamfer_dimension` accepts a chamfer edge SID or visible sheet anchor, with optional `feature_ref`, and reads symmetric or distance-angle sizes from the model; other chamfer types are refused. `add_ordinate_dimensions` creates baseline/ordinate sets; for `axis="both"` supply positions as `{"x":[...],"y":[...]}`.
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
```

