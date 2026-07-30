---
name: make-drawing
description: Produce 2D drawings with correct view order, measured dimensions, GD&T, title block, and export.
inject: auto
triggers:
  keywords: [drawing, dimension, GD&T, title block, section view, detail view]
  tools: [make_drawing, add_dimension, add_drawing_view, export_drawing]
  phases: [drawing, deliver]
---

# Make Drawing

How to produce a manufacturing 2D drawing from a part in Chudware. Views first, measured dims only, finish with title block and export.

## Sequence

1. **Orient** — `list_parts()` / `describe_part(doc_id)` on the source solid. Capture critical dims via `measure_part` / `query_geometry` before any annotation. Never invent sizes.
2. **Create sheet** — `make_drawing(...)` for a fresh drawing tied to the part. Keep the returned drawing id for every later call.
3. **Views in order** — place geometry before annotations:
   - Base view first (`add_drawing_view`)
   - Projected orthographic views next
   - Section views (`add_section_view`) for hidden interiors
   - Detail views (`add_detail_view`) for tight features
4. **Dimension from truth** — `add_dimension` only with entity ids and values taken from `describe_part` / `measure_part`. No guessed numbers, no freehand sizes.
5. **Callouts** — GD&T via `add_gdt`; surface finish via `add_surface_finish`. Apply only where the design intent needs control (mates, seals, bearing seats).
6. **Title block** — `set_titleblock` with title (the part's NAME — never `part_number`, that cell is the identifier a shop orders from), revision, material, finish, scale, and author/date as known. There is no units field: units are a document property (mm by default), so `units=` is refused.
7. **Export / handoff** — `export_drawing` (or an explicit user-facing summary of sheet contents) before declaring done.

## Rules

- **Views before dims.** Never dimension empty space or missing views.
- **Measured, never invented.** Every linear/angular size comes from geometry query tools.
- **Verify-before-done.** Confirm views, dims, GD&T, title block, then export or summarize.
- **One source part.** Drawing annotations must match the live solid; re-measure after late model edits.

## Deeper docs

- Drawing tool vocabulary and standards notes: context pack `drawing_standards`
- Part measurement: `describe_part`, `measure_part`, `query_geometry`
