# Contract: Fabrication Outputs + 3D Bridge — v1

> Manufacturing exports and the headline **PCB → 3D solid** bridge into the CAD app. All writers are
> pure functions `BoardDocument → file(s)`; no external tools required (GPL-clean, native writers).

## 1. Gerber (RS-274X) — `services/ecad/fab/gerber.py`

- One file per fabrication layer; standard extensions: `.GTL/.GBL` (copper top/bottom),
  `.GTO/.GBO` (silk), `.GTS/.GBS` (mask), `.GTP/.GBP` (paste), `.GM1`/`.GKO` (outline/Edge.Cuts),
  inner copper `.G2,.G3,...`. A `.gbrjob` job file is optional (follow-up).
- Format: `%FSLAX46Y46*%` (4.6, absolute), `%MOMM*%` (mm). Apertures via `%ADD10C,0.25*%`
  (circle), `R` (rect), `O` (obround), `%AMxxx*%` macros for roundrect/custom pads. Regions
  (`G36/G37`) for zones and copper pours; `D01/D02/D03` for draw/move/flash.
- Track → aperture-`D01` strokes; pad → flash (`D03`) of its aperture; zone filled polys → region
  fills; silk/fab graphics → strokes/regions on their layer.
- **Test:** every emitted file lexes against an RS-274X grammar test (aperture defs precede use;
  balanced `G36/G37`; `M02*` terminator); aperture count and flash count match the board's pad
  count per layer.

## 2. Excellon drill — `services/ecad/fab/excellon.py`

- One PTH file + one NPTH file (or combined w/ `M48` header + tool table). Header `METRIC,TZ`,
  tool defs `T1C0.600`, `T01` select, `X..Y..` coordinates, `M30` end. Round drills as hits; ovals
  as routed slots (`G85`). Plated vs non-plated separated.
- **Test:** tool diameters == distinct pad/via drill sizes; hit count == drilled pad+via count.

## 3. BOM — `services/ecad/fab/bom.py`

- Group rows by (`value`, `footprint`, sorted user fields), `dnp` excluded by default. Columns:
  `Refs, Qty, Value, Footprint, MPN, <user fields>`. CSV + JSON. Stable ref sorting (`R1,R2,R10`
  natural order). **Test:** N parts → correct grouping & quantities; DNP excluded.

## 4. Pick-and-place (CPL) — `services/ecad/fab/placement.py`

- CSV: `Designator, MidX(mm), MidY(mm), Layer(Top/Bottom), Rotation(deg)`. Position = footprint
  origin in board coords (drill/place origin applied). Bottom parts rotation mirrored per convention.
  THT-only / `virtual` excluded by default. **Test:** placements match footprint `pos/angle/side`.

## 5. 3D bridge — `services/ecad/threed.py` (the integration that makes this one program)

`board_to_step(board, opts) -> {step_path, summary}` and `board_to_part_doc(board, opts) ->
aicad.part/1 doc` (a thin CAD document whose single `import` feature references the STEP):

1. **Substrate**: extrude the `Edge.Cuts` outline polygon to `stackup.thickness` (centered on z=0
   or z=0..t per `opts.z_origin`). Subtract plated/NPTH holes (cylinders at pad/via drills). Color =
   board solder-mask color (`opts.mask_color`, default green).
2. **Copper (optional, `opts.copper=true`)**: thin (0.035 mm) extrusions of tracks/pads/zones per
   copper layer, on top/bottom faces; gold/HASL color. Off by default (heavy); bounding realism only.
3. **Components (`opts.components=true`, default)**: for each `on_board` footprint, load its
   `model_3d` (STEP via existing `engine/doc/formats.import_path`; WRL → skipped w/ warning) and
   place it at `(pos, angle, side)` with the footprint→3D offset from the library. **Fallback**: no
   model → an extruded courtyard-sized box proxy at the part's height (`fields.height` or a
   per-`attr` default) so the enclosure designer still has a collision volume.
4. **Output**: write `geometry.step` (a compound: board + components) and return an `aicad.part/1`
   doc `{features:[{type:"import", params:{path:step_path, name:"PCB"}}]}` so `ecad_to_3d` can open
   it directly in the CAD viewport. The user then models the housing around it and uses the existing
   **assembly interference/fit report** to verify clearance.

Units mm throughout (matches CAD canonical). **Test:** board solid volume ≈ outline_area ×
thickness − holes (±1%); a placed proxy component's centroid == its footprint world position;
the produced part doc opens via the CAD `import` feature and yields a non-empty mesh.

## 6. KiCad / SPICE / netlist export

- `kicad`: write `.kicad_pcb` (board) and a flat netlist — best-effort, lossy-documented.
- `spice`: write the simulation deck (from the netlist + sim params) — round-trips with `parse_spice`.
- `netlist`: a generic KiCad-style `(export (components ...) (nets ...))` s-expr for external tools.

## 7. Schematic outputs — `core/ecad/schexport.{h,cpp}` + `core/ecad/schplot.{h,cpp}`  (v1.1)

§1–§6 are all **board** outputs. A schematic could export nothing at all: `ecad_export` rejected
every kind that was not `gerber|drill|bom|cpl` and each of those required a pcb document. These are
the schematic's deliverables, reachable through the same `ecad_export {doc_id, kind, path?, opts?}`.

| kind | writer | output |
|---|---|---|
| `netlist` | `fab::write_sch_netlist(sch, netlist)` | KiCad-style `(export (components …) (nets …))` s-expr for external tools. Nets sorted by name, nodes by natural ref order, `code` a 1-based index. DNP parts included — a netlist is electrical, not assembly |
| `bom` | `fab::write_sch_bom(netlist, fmt, exclude_dnp)` | CSV (RFC4180) or JSON. Grouped by (value, footprint, sorted user fields); `Refs,Qty,Value,Footprint,Datasheet,MPN,<user fields>`. `in_bom == false` is ALWAYS dropped; `dnp` is dropped when `exclude_dnp` |
| `spice` | `fab::write_spice_deck(sch, netlist)` | ngspice deck; nodes ordered by the sim mapping's `pin_order`, `GND`/`0`/`GNDA`/`VSS` forced to node 0, unmapped parts skipped with a named comment. Round-trips through `parse_spice` |
| `kicad` | `kicad::export_kicad_sch(sch)` | `.kicad_sch` S-expression (KiCad 7). +y-up document coords are negated into KiCad sheet space. Best-effort and documented-lossy in the same way the importer is |
| `svg` | `plot::schematic_svg_pages(sch, opts)` | One true-size SVG per sheet (`width`/`height` in mm with a matching `viewBox`), keyed by sheet id |
| `pdf` | `plot::schematic_pdf(sch, opts)` | One self-contained multi-page vector PDF, one page per sheet, Helvetica text, no raster and no external tool |

`opts` for `svg`/`pdf`: `{sheet?, frame?, title_block?, pin_numbers?, pin_names?, paper?, margin_mm?}`.
Both plot formats render through **one shared `Painter` abstraction** so the SVG and the PDF cannot
drift, and both draw the same entity set the canvas does: sheet frame + zone ruler, title block,
graphics, buses and bus entries, wires, junctions, no-connects, symbol bodies with pin
decorations/numbers/names, reference and value, power-port glyphs, scope-specific label outlines,
sheet symbols with their pins, and free text.

**`path` semantics (all kinds).** A directory-looking `path` receives one conventionally-named file
per logical key (`<stem>.GTL`, `<stem>-PTH.drl`, `<stem>-bom.csv`, `<stem>.net`, `<stem>.cir`,
`<stem>.kicad_sch`, `<stem>-<sheet>.svg`, `<stem>.pdf`); a file-looking `path` receives a single
output directly. The response's `paths` lists exactly what was written and is **empty when `path`
was omitted** — callers report from `paths`, never from the absence of an error.
