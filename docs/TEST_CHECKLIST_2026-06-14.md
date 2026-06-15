# Test Checklist — 2026-06-14 host-exposure + features session

Build is synced: run `dist\AI-CAD\AI-CAD.exe` (dist payload + the `%APPDATA%\AI-CAD\app` worker are
both re-staged with all engine changes). 22 commits this session; engine 680 tests green; every form
verified headlessly. This checklist is for **your** hands-on pass — SolidWorks vocabulary, expected
results, and the gotchas worth poking at.

Legend: **[P]** = pass, **[F]** = fail (note what), **[?]** = looks off.

---

## 1. Part Features — PropertyManager option matrices

For each, start a sketch on a plane, draw a profile, then invoke the feature from the **Features** tab.

### Extrude Boss/Base
- [ ] End conditions combo now has **Up To Surface / Offset From Surface / Up To Body / Up To Vertex**
      (in addition to Blind / Mid Plane / Through All / Up To Next). Selecting a geometry-driven end
      enables an **"Up To Reference"** well (pick a planar face / vertex) and Offset enables an **Offset** field.
- [ ] **Direction 2** has the same end-condition list + its own Up To Reference / Offset.
- [ ] **Thin Feature** group: check it → Wall thickness + Wall side (One/Two/Mid). Makes a thin wall.
- [ ] Up To Surface: extrude a probe down onto a face → it lands exactly on that face.

### Revolve Boss/Base
- [ ] **End Condition** combo: One Direction / **Mid Plane** / **Up To Surface** (was angle-only before).
- [ ] **Direction 2** checkbox + Angle; **Thin Feature** (thickness + Inside/Outside/Both).

### Sweep / Loft
- [ ] Sweep: **Guide Curves** well, **Profile Orientation** (Follow Path / Frenet / Keep Normal),
      **Twist (turns)** + **Twist (degrees)** — note Twist (turns) shows a bare number, not "0mm".
- [ ] Loft: **Centerline** well, **Guide Curves** well, **Start/End Constraint** (None / Normal To
      Profile / Tangent To Section / Curvature).

### Fillet / Chamfer
- [ ] Fillet Type combo now includes **Face Fillet** → two **Face Set** wells appear; **Tangent
      propagation** is a real checkbox (was "coming soon").
- [ ] Chamfer Type combo: Distance (Symmetric) / Distance (Asymmetric) / **Angle-Distance** /
      **Vertex**. Vertex → a **Vertices** well; Angle-Distance → an **Angle** field (shows "deg", not
      "mm") + optional **Reference Face**.

### Draft / Mirror / Pattern / Hole
- [ ] Draft: **Draft Type** combo (Neutral Plane / **Parting Line** / **Step**) → parting modes show a
      **Parting Line (edges)** well.
- [ ] Mirror: **Bodies to Mirror** well + **Merge result** (alongside Features to Mirror).
- [ ] Linear/Circular Pattern: an **Instances to skip** text field (linear: `2:1, 3:0`; circular:
      `1, 3, 5`) → those instances are omitted.
- [ ] Hole Wizard: Size combo now lists **ANSI inch** (#4-40 UNC … 1/2-13 UNC, #10-32 UNF, 1/4-28 UNF)
      and **NPT pipe** (NPT 1/8 … 1/2) alongside metric. Pick one → the hole uses that standard's drill.

## 2. Surfacing (new **Surfaces** ribbon tab)
- [ ] A **Surfaces** tab appears (rightmost). It has Create (Extruded/Revolved/Swept/Lofted/Planar
      Surf.), Curves & Patches (Offset/Ruled/Boundary/Filled), Edit (Knit/Thicken/Trim/Untrim/Delete
      Face/Replace Face). Each opens a PropertyManager form with the right selection wells.
- [ ] Extruded Surface from a sketch → an open surface body (no end caps). Thicken → back to a solid.

## 3. Assemblies — advanced / mechanical mates
Open an assembly, insert 2+ components, **Mate**. The mate form now has a **Mechanical & Advanced
Mates** section.
- [ ] Buttons: Gear, Screw, Rack & Pinion, Distance Limit, Angle Limit, Hinge, Slot, Cam Follower,
      Profile Center, Width, Symmetric.
- [ ] **Gear**: pick two cylindrical faces/axes → a **Ratio** field appears; set 2 → turning one part
      counter-rotates the other at 2:1.
- [ ] **Screw** → Pitch; **Distance/Angle Limit** → Min + Max; **Width / Symmetric** → a **Reference
      3** well appears (the jaw / symmetry plane).

## 4. Import / Export (File menu)
- [ ] **File ▸ Export**: STEP / IGES / STL / 3MF each open a Save dialog and write the file; **DXF
      (flat pattern)** works on a sheet-metal part with a flat pattern (clean error otherwise).
- [ ] **File ▸ Open** a plain `.step` / `.stl` / `.iges` / `.3mf` (not a `.step+`) → it imports as a
      new part (used to fail). Re-open one of the files you just exported.

## 5. Equations / Global Variables (P0 — new)
- [ ] Open via **View ▸ Equations** OR double-click the **Equations (N)** node in the FeatureManager.
- [ ] **Add** a global, e.g. `width = 20`, then `height = width * 1.5`. They list with editable
      equations + delete (X) buttons.
- [ ] In a sketch/feature dimension, type `= width` (or `width/2`) → it binds to the global; editing
      the global rebuilds the model.
- [ ] Delete a global that a feature uses → a clean "in use" toast, not a crash.

## 6. Evaluate ▸ Mass Properties / Measure
- [ ] Mass Properties now has a **Material** preset (Steel/Aluminum/Titanium/Brass/ABS/PLA/…) + an
      editable **Density (g/cm³)**. Switch Steel→Aluminum → Mass drops ~2.9× (7.85→2.70). Editing
      density selects Custom.
- [ ] Mass Properties also shows **Principal axes** (Px1/Px2/Px3 = principal moment + its unit axis
      vector) below the inertia tensor — scroll the dialog if the pane is short.
- [ ] Measure: a circular edge now also shows **diameter** (= 2×radius). Repeated measurements
      accumulate in a **History** list (newest first) with a **Clear** button.

## 7. Chrome / sizing (quick visual)
- [ ] Settings dialog, title-bar caption buttons, the view-cube, the viewport HUD toolbar + nav gizmo,
      the AI Copilot panel, and PropertyManager OK/Cancel chips all look crisp at your monitor DPI
      (no clipped/oversized elements). Try `AI_CAD_SCALE=1.5` for a HiDPI pass if you want.
- [ ] Angle fields everywhere read e.g. `45deg` / `360deg` (not `45mm`).

---

### Notes / regressions found
(jot here as you go)
