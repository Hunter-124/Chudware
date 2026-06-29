# Contract: Fabrication Outputs + 3D Bridge — v1

> Manufacturing exports and the headline **PCB → 3D solid** bridge into the CAD app. All writers are
> pure functions `BoardDocument → file(s)`; no external tools required (GPL-clean, native writers).

## 1. Gerber (RS-274X) — `fab/gerber.py`

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

## 2. Excellon drill — `fab/excellon.py`

- One PTH file + one NPTH file (or combined w/ `M48` header + tool table). Header `METRIC,TZ`,
  tool defs `T1C0.600`, `T01` select, `X..Y..` coordinates, `M30` end. Round drills as hits; ovals
  as routed slots (`G85`). Plated vs non-plated separated.
- **Test:** tool diameters == distinct pad/via drill sizes; hit count == drilled pad+via count.

## 3. BOM — `fab/bom.py`

- Group rows by (`value`, `footprint`, sorted user fields), `dnp` excluded by default. Columns:
  `Refs, Qty, Value, Footprint, MPN, <user fields>`. CSV + JSON. Stable ref sorting (`R1,R2,R10`
  natural order). **Test:** N parts → correct grouping & quantities; DNP excluded.

## 4. Pick-and-place (CPL) — `fab/placement.py`

- CSV: `Designator, MidX(mm), MidY(mm), Layer(Top/Bottom), Rotation(deg)`. Position = footprint
  origin in board coords (drill/place origin applied). Bottom parts rotation mirrored per convention.
  THT-only / `virtual` excluded by default. **Test:** placements match footprint `pos/angle/side`.

## 5. 3D bridge — `engine/ecad/threed.py` (the integration that makes this one program)

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
