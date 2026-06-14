# AI-ECAD — GUI hands-on test checklist

A click-through checklist for the interactive ECAD editor (Waves N–Y), written for a hands-on tester.
Each item is a single action with the expected result. Build the app, launch `AI-CAD.exe`, and click
the **PCB** ribbon tab to start. The ECAD editor is docked as a tab next to the 3D **Viewport**.

> Engine note: the worker runs STAGED code at `%APPDATA%\AI-CAD\app`. If you rebuilt the Python
> engine, re-stage with `<venv> -m installer.provision --with-app-code` before testing. GUI-only
> rebuilds don't need it.

## 1. AI design (the launchpad)
- [ ] PCB tab → **AI Design** (or open the ECAD tab with no doc). The launchpad shows a prompt box.
- [ ] Type `5V divider with 2.5V output` → **Design with AI**. A schematic appears (symbols + wires +
      net labels) within a couple seconds. The header reads **SCHEMATIC**.
- [ ] Try `inverting amp gain 10`, `RC low-pass 1kHz`, `555 astable` — each yields a valid schematic
      (recipe fallback means no model is required).

## 2. AI Board (one click to a routed PCB)
- [ ] On the launchpad, type a request → **AI Board**. The flow runs design → populate → outline →
      autoroute and lands on a **PCB LAYOUT** with footprints, a rectangular board outline, and a
      copper track between the parts. Toast: "AI board ready — Board to 3D builds the enclosure".

## 3. Edit tools (Select / Place / Route)
- [ ] **Place** + pick a part (e.g. `Device:R`) → click the canvas. A symbol drops on the grid.
- [ ] **Route** → click points; **Esc** / right-click finishes the wire/track.
- [ ] **Select** → click a part. It gets a highlight halo.
- [ ] Drag the selected part — it moves on release, grid-snapped (1.27 mm sch / 0.5 mm board).
- [ ] **Delete** key (mouse over canvas) removes the selected part.

## 4. Property inspector (Ref / Value)
- [ ] Select a resistor. A second toolbar row shows its lib id + editable **Ref** and **Value** fields.
- [ ] Change Value `10k` → `4k7`, press **Enter** (or **Apply**). The label on the canvas updates.
- [ ] Select a different part — the fields reload to that part's values (no bleed-through).

## 5. Rotate / flip (KiCad-style R / F)
- [ ] Select a part, press **R** (mouse over canvas) — it rotates 90° CCW. Repeat to spin it around.
- [ ] Press **F** — a symbol mirrors; a footprint flips to the other board side.
- [ ] The inspector's **Rotate** / **Flip** buttons do the same.

## 6. Undo / redo
- [ ] Make an edit, click **Undo** (or **Ctrl+Z** with the ECAD tab focused) — it reverts.
- [ ] **Redo** (or **Ctrl+Y** / **Ctrl+Shift+Z**) re-applies. (These are gated to the ECAD window so
      they never disturb the 3D CAD doc's own undo.)

## 7. Simulation (the "improve on KiCad" mandate)
- [ ] Open/AI-design a schematic with a source (e.g. RC low-pass). Pick **Transient** → **Simulate**.
      The bottom **Simulation** tab plots V/I traces.
- [ ] Switch to **AC** → **Simulate**. The plot shows a Bode magnitude rolloff (a unit AC stimulus is
      injected automatically when the source carries none).

## 8. PCB → 3D (the housing bridge)
- [ ] On a board, click **Board to 3D**. The 3D **Viewport** surfaces with the board as a solid part
      (substrate slab + component proxies). Toast: "Board imported into 3D — design the housing…".
- [ ] The board is now a normal CAD part — use the modeling tools to build an enclosure around it.
- [ ] End-to-end: **AI Board** then **Board to 3D** takes a sentence all the way to a 3D board solid.

## 9. Save / Open
- [ ] **Save** → choose a path. A native `*.ecad+` file is written.
- [ ] New/blank, then **Open** → pick the `.ecad+` — the design reloads exactly (parts, wires, nets).
- [ ] **Open** a KiCad `*.kicad_sch` / `*.kicad_pcb` — it imports into the editor.

## 10. Fabrication outputs
- [ ] On a board: **Export Gerber**, **Export Drill**, **Export BOM** write into
      `%APPDATA%\AI-CAD\ecad_out`. (Gerber per-layer + Excellon + CSV BOM.)

## 11. Layers / nets / DRC-ERC
- [ ] Toggle the left **Layers** (board) / **Nets** (schematic) panel via the header checkbox.
- [ ] The bottom **ERC** / **DRC** tab lists violations with messages; ratsnest airwires show unrouted
      nets on the board.
- [ ] Click a violation row that has a location — the canvas pans + zooms to it and flashes a
      crosshair marker (hover shows "Click to zoom to (x, y)").

---
Headless equivalents (for scripted verification): `AI_CAD_SHOT=ecad` with `AI_CAD_ECAD_KIND=` one of
`ai, sch, pcb, 3d, editsch, select, del, sim, undo, aiboard, rotate, aiboard3d, saveopen, violzoom`
(+ `AI_CAD_THEME=dark|light`), run as `AI-CAD.exe --ui-screenshot out.png`.
