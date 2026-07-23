# T-BL41 — Sketch annotation auto-hide: verification record + limitation

Branch: `task/T-BL41-sketch-dimension-autohide` (stacked on `task/T-BL48-wire-imgui-sketch-entry`)

## What the change does
In sketch edit mode the dimension + geometric-relation annotation glyphs are
**hidden by default** (selection tool / `SketchTool::None`) and are **revealed only
while the SmartDim (dimension/relation) tool is armed** (`SketchTool::SmartDim`).
Hiding is rendering-only — the solver and constraint data are never touched, so a
constrained point still obeys its constraints when dragged.

With SmartDim armed the user can also:
- **Add** a relation: select geometry → right-click "Add Relation..." → pick a type
  (`sketch_add_constraint`), or dimension via the SmartDim pick flow.
- **Delete** a relation/dimension: click its glyph to select it → Delete
  (`sketch_delete_constraint`). Both engine ops were already fully wired — no engine
  gap, so no blocking question.

## Screenshot evidence (PROVEN NOW, headless, engine attached)
Captured via a new deterministic `CHUDWARE_SHOT=sketch_autohide` scenario
(`ui/imgui/src/ui/scenarios.cpp`) that builds a genuinely constrained sketch
(80×40 rectangle with per-edge horizontal/vertical + chained coincident relations
AND a driving 80 mm distance dimension), enters the EXISTING sketch via the real
dispatch→drain path (`pendingSketchEditFid` → `enterForFeature`, `inSketch=1`), then
captures two static states. Run engine-attached under xvfb (never demo mode):

    CHUDWARE_SHOT=sketch_autohide CHUDWARE_LIBCHUDWARE=<core .so> \
      xvfb-run -a build/linux-app/bin/AI-CAD2 [--light] --shot <path> --frames 400

Artifacts (committed under `docs/sessions/assets/`):
- `bl41_{dark,light}_tooloff.png` — SmartDim OFF: clean sketch geometry, NO dimension
  text, NO relation badges (auto-hidden). "Under Defined DOF: 3" badge confirms a
  real, solver-driven sketch is being edited.
- `bl41_{dark,light}_toolon.png` — SmartDim ON: the 80 mm **dimension** (extension +
  dimension line with arrowheads) and the **coincident relation badge** ("o" glyph)
  are revealed. Banner switches to the SmartDim pick prompt.

Eyeballed: all four confirm tool-OFF = no glyphs, SmartDim = dimension + relation
glyphs shown, identical in light and dark.

Harness note (not a product defect): a rounded demo-doc solid renders behind the flat
sketch because the viewport still meshes the startup demo document while the sketch
overlay draws on top; it does not affect the annotation-visibility behavior under test.

## VERIFICATION LIMITATION — human eyeball at merge
The INTERACTIVE half of the DoD cannot be driven by a static `--shot` scenario:
- **Add a relation** (select geometry → apply) with the tool active,
- **Delete a relation** (click glyph → remove) with sketch solve updating,
- **Drag a constrained point** and confirm it still obeys.

These require live mouse input into the ImGui event loop, which the headless shot
harness does not synthesize (no xdotool click-injection wired for this build, and the
branch is stacked on the not-yet-merged T-BL48 sketch-entry wiring). The code paths
ARE in place and were confirmed by reading:
- badge click → `toggleRel` → `selRelIds_`; Delete → `sketch_delete_constraint`
  (sketch_overlay.cpp), now reachable in SmartDim mode.
- right-click "Add Relation..." → `relIds_`/`sketch_add_constraint`, reachable in
  SmartDim mode.
- solver re-run via `resolve(st)` after every constraint change; drag path untouched
  (None-mode entity drag preserved), so constrained-point-obeys is satisfied by
  construction (rendering-only change).

**Action for the human merging:** in a running GUI, enter a constrained sketch, arm
Smart Dimension, (1) click a relation badge and press Delete → confirm it is removed
and the sketch re-solves; (2) select two edges → right-click → Add Relation →
Parallel → confirm it applies; (3) with the tool off, drag a constrained endpoint →
confirm it still respects its constraints. This is the only part not machine-proven.
