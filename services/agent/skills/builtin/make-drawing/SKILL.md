---
name: make-drawing
description: Produce 2D drawings with correct view order, measured dimensions, GD&T, title block, and export.
inject: auto
triggers:
  keywords: [2D drawing, engineering drawing, dimensioned drawing, save drawing, native drawing, GD&T, title block, section view, detail view, drawing sheet]
  tools: [make_drawing, add_dimension, add_drawing_view, export_drawing]
  phases: [drawing]
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
4. **Dimension from truth** — `add_dimension(view_id, a, b?, dtype=...)` anchors to model SIDs/feature names or measured sheet-mm points from the drawing view. For `add_hole_callout`, use `a=[x,y]` sheet mm at or near the projected circle or centre, not a model SID. Confirm sizes against geometry queries; never substitute a guessed `text` override for a measured dimension.
5. **Callouts** — GD&T via `add_gdt`; surface finish via `add_surface_finish`. Apply only where the design intent needs control (mates, seals, bearing seats).
6. **Title block** — `set_titleblock` with title (the part's NAME — never `part_number`, that cell is the identifier a shop orders from), revision, material, finish, and author/date as known. Set geometric `scale` on `make_drawing`/`arrange_drawing`, not just printed scale text. Units are a document property (mm by default), so `units=` is refused.
7. **Review / handoff** — `review_drawing` after edits. A requested non-release draft may be exported via `export_drawing` without a release audit; label it a draft. For release, `audit_drawing` against real manufacturing requirements, pairing `view_id` with `annotation_id`; an empty spec is refused. Release only with `verdict="release_ready"` and no `missing_critical`.
8. **Native persistence** — save the standalone drawing with `export_part(fmt="draw+", doc_id=<drawing_id>, path="sheet.draw+")`; `save_part` takes the open source part/assembly, not a drawing id, and embeds its associated drawings in `.step+`.

## Rules

- **Views before dims.** Never dimension empty space or missing views.
- **Measured, never invented.** Every linear/angular size comes from geometry query tools.
- **Verify-before-done.** Confirm views, dims, GD&T, title block, then export or summarize.
- **One source part.** Drawing annotations must match the live solid; re-measure after late model edits.

## Deeper docs

- Drawing tool vocabulary and standards notes: context pack `drawing_standards`
- Part measurement: `describe_part`, `measure_part`, `query_geometry`
