# Hands-on test checklist — gap-close session (2026-06-13)

Run `dist\AI-CAD\AI-CAD.exe` (dist is synced; worker app code re-staged). Three new capabilities to
exercise, all SolidWorks-equivalent flows.

## 1. Assembly — drag-move a component (SW: Move Component)
1. Open or build an assembly (multiple components with at least one under-defined mate).
2. Select a single component in the viewport.
3. Click-drag it. It should **pull toward the cursor along its free degrees of freedom only**
   (constrained DOF stay locked), updating live, then settle on release as **one** undo step (Ctrl+Z
   returns it in a single press).
4. Press **Esc mid-drag** → it snaps back to where it started (revert).
5. Try dragging a **fully-constrained / fixed** component → expect a toast and no movement.

## 2. Assembly — component pattern (SW: Linear/Circular Component Pattern)
1. In an assembly, invoke the **component pattern** command (Assembly tab).
2. In the PM form: pick **Linear** or **Circular**, choose a **direction/axis** (e.g. `axis.x`,
   `plane.right`), set **count** + **spacing (mm)** / **angle (deg)**, then click components in the
   viewport to add them to the **seed list**.
3. Confirm → the seeded component is replicated into a grid/ring of instances at the right spacing.
4. Open a **drawing BOM** of the assembly → the patterned instances should roll up into the seed
   part's **quantity** (one row, qty N), **not** appear as separate `part#1`, `part#2` rows.
5. **Known limit:** direction/axis must be a datum/global axis string for now — picking a model
   edge/face as the pattern direction is deferred.

## 3. ECAD — symbol property inspector (footprint / DNP / sim-params)
1. Open the PCB/ECAD editor, load or AI-design a schematic, pick the **Select** tool, click a symbol.
2. The inspector row now shows, after Rotate/Flip:
   - **Footprint** — type a footprint id (or use the **Pick** dropdown), click **Set**. Blank + Set
     **clears** the assignment.
   - **DNP (do-not-populate)** — toggling immediately marks/unmarks the part; it should drop out of
     the BOM (`exclude_dnp`) and be skipped by the simulator.
   - **Sim params (JSON)** — enter a JSON object (e.g. `{"model":"..."}`) and click **Apply**; bad
     JSON shows a toast and does not commit; blank + Apply clears it.
3. Switch between two different symbols → fields must repopulate per part (no value bleed).

## Decisions locked (no test, FYI)
- **Clean-room / GPL-clean: confirmed** — KiCad source is never copied; features are reimplemented
  clean-room. (Owner sign-off in `contracts/licenses.md`.)
- **ngspice** — fine as an optional, unmodified, subprocess-only backend; native MNA stays primary.
- **AI cloud opt-in** — already works: set `offline_mode` false in Settings to allow cloud models;
  true keeps everything local (your current setting).

## Deferred (scoped, not built — large)
- **ECAD multi-doc** (open several boards/schematics in tabs) — ~30-site host refactor.
- **Spline on Surface** — needs new 3D-on-face point picking + surface projection; multi-day.
