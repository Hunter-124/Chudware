# Drawing standards context pack

## View placement order
- Base view first, then projected orthographics (top/front/right as needed)
- Section views for internal features; detail views for tight geometry
- Tools: `make_drawing`, `add_drawing_view`, `add_section_view`, `add_detail_view`
- Never dimension until the needed views exist

## Dimensioning
- Pull sizes from `describe_part` / `measure_part` / `query_geometry` — never invent
- Prefer functional dims (mates, hole patterns, overall envelope) over every edge
- Avoid duplicate or chain-overconstrained dims; one path per critical size
- Tool: `add_dimension` with real entity ids from the drawing/part

## GD&T and finish (brief)
- Common: Ⓟ position, Ⓕ flatness, Ⓟ perpendicularity, Ⓟ parallelism, Ⓒ concentricity, Ⓟ profile
- Datum scheme: primary on primary mating face, secondary/tertiary to lock remaining DOF
- Surface finish via `add_surface_finish` on sealing, bearing, or cosmetic faces
- Tool: `add_gdt` only where process/fit needs control

## Title block and export
- `set_titleblock`: part name, rev, material, units, scale, drawn-by/date
- Finish with `export_drawing` or an explicit sheet summary to the user
- Re-measure after late solid edits before releasing the sheet
