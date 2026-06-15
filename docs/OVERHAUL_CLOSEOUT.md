# SolidWorks-Parity Overhaul — Close-out (C52+, 2026-06-12/13)

A single autonomous run delivering SolidWorks-class parity across modeling, the sketcher,
Instant3D, the UI platform, and four new modules. Plan: `.claude/plans/i-want-you-to-zesty-thimble.md`.
Baseline before this overhaul: `cf5f299` (the C51 sketched-feature checkpoint).

## Waves delivered (each = build + manual SolidWorks checklist + commit on master)

**Foundation**
- W1 Hole Wizard PM form (ISO standard table + multi-position grid/vertices; `omitWhenDisabled`).
- Datum-plane convention fix — host `planeCorners` now matches engine Z-up datums (Front/Top were
  swapped, so the highlighted plane ≠ the plane the click sketched on). *(User-reported bug.)*
- W3 Booleans + Helix compilers & forms; body-selector wells (`PMSelKinds.body`, resolvePick body).
- W4 Reference Geometry forms + the deferred datum methods (angle/3-point plane, cylinder/edge/
  intersect axis, point methods, csys); topology-dedup fix (`_describe_pool`).
- Wrong-topology pick feedback toast.
- W5 Solver-hardening gate — rewrote `nsolve._drag_solve` (dense-pinv → sparse KKT-Schur); dragging
  was slow AND truncating before tracking the cursor; now 6.4 ms @208 unknowns, exact.
- W6 Sketch fillet/chamfer; W7 sketch mirror / linear+circular pattern / convert entities.
- W8 Instant3D face push-pull (`doc_push_pull`); W9 Instant3D dimension-handle drag.
- W10 multi-document file tabs; W11 Document Units (MMGS/IPS) toggle + reorganized Settings.

**Modules**
- W12 Drawings — `aicad.drawing/1` doc kind, HLR 2D views, 2D sheet panel (Make Drawing).
- W13 Drawing dimensions (value = true model length, scale-independent) + assembly BOM.
- W14 2D export — native R12 DXF + hand-rolled vector PDF (drawing sheets + flat-pattern flat DXF
  with bend lines on their own layer).
- W15 Sheet Metal — base/edge flange with real bend allowance (`BA = θ(R+KT)`); ribbon tab.
- W16 Sheet Metal flat-pattern unfold (developed length uses BA, verified 60.5553 mm) + Flatten.
- W18 Weldments — structural members (6 profiles) + mitre corner trim; W19 cut list.
- W20 Configurations — per-config param overrides + suppression (non-mutating compile overlay);
  switcher panel. W21 Design Table grid.

**Post-roadmap follow-ups**
- W3b Rib + Split features (last two greyed Features-tab buttons): rib = thicken an open contour →
  trim/fuse support wall; split = divide body(ies) by a planar face/datum into above/below.
- *Hollow-slot extrude bug* — `_detect_loops` now reverses end-matched edges so a decomposed slot
  (2 lines + 2 cap arcs, as the Slot tool emits) winds consistently and is classified as a hole, not
  filled material. *(User-reported bug.)*

**W23 — greyed-button close-out sweep** (every remaining disabled ribbon command audited)
- W23a Fix relation (one-click pin of selected sketch entities; engine `fix` was already
  solver-backed) + Geometry Analysis un-greyed (its handler already mapped to the real `eval_check`).
- W23c Zebra Stripes — real reflected-stripe GLSL surface-continuity mode (`uZebra`), replacing a
  deferred stub; live density slider, exits cleanly.
- W23b Sketch Text (build123d `Text`, cap-height-rescaled, glyph holes preserved) + Equation-Driven
  Curve (safe-AST parametric `x(t)/y(t)`, spline interp). Engine + host tools.
- W23d Style Spline (control-polygon B-spline by poles) + **solver hardening**: an adversarial check
  found `_allocate_entity` raises on entity types it doesn't special-case and `solve_sketch` doesn't
  wrap it — so an `eq_curve` (W23b) or `style_spline` in a live sketch crashed on the first drag.
  Fixed across all three solver entity-dispatch sites (alloc / handle / emit).
- The *only* remaining greyed command is **Spline on Surface** — deliberately deferred: it needs 3D-
  sketch-on-face projection infrastructure (a curve constrained to lie on a model surface), a
  separate focused effort, not a quick un-grey.

## Contract version-bump ledger (both additive — zero break for existing docs)

| Change | Kind | Backward-compat proof |
|---|---|---|
| `aicad.drawing/1` (W12) | New top-level doc kind | part/asm schemas byte-for-byte unchanged |
| `configurations[]` + `active_configuration` (W20) | Optional fields on part/asm | absent ⇒ implicit "Default", compiles byte-identically (regression-guarded test) |

All other waves were **append-only** within existing contract sections (new ops/RPCs/feature
sections/PM fields) — consistent with how the codebase already grew (`solve_rejected`, `owner_map`,
C26-B extrude additions).

## Regression state (close-out)

- **Core `engine/doc`: 377 passed** — every wave added pytest-collectable tests to the gate.
  (As of the GUI-rework session: **470 passed**, incl. Wave 4A/4B copy/paste ops.)
- Broad `engine + agent`: **429 passed**. The only failures were **pre-existing/environmental**,
  identical to the pre-overhaul baseline and outside any code this overhaul touched:
  `engine/sim/test_fea.py` (3, gmsh `WindowsPath`), `engine/sim/test_lbm.py` (2 errors),
  `agent/test_compaction.py` (2, offline-mode config). **The overhaul introduced zero regressions.**
- **UPDATE (GUI-rework session, 2026-06-14):** those reds are now resolved. The `engine/sim`
  ones were a *misdiagnosis* — not gmsh/environmental but the house-style `main()` runner threading
  shared state (meshed STEP path; sphere-drag result) that pytest collection never executes; fixed
  with module-scoped fixtures (no skips, no weakened asserts) → **`engine/sim` 60 passed**. The
  `agent/test_compaction.py` pair was already fixed by commit 5551635 (`offline_safe=True` escape
  hatch); the line above was stale → **`agent` 40 passed**.
- Host compiles clean every wave; `dist\AI-CAD` synced.

## Known stretch / deferred (honest)

- Sheet Metal: jog / hem / miter / tab features (core flange + flat pattern + flat DXF shipped).
- Weldments: gusset / end-cap / weld-bead geometry (members + mitre trim + cut list shipped).
- Patterns: edge / cylindrical-face axis pickers (global X/Y/Z datums shipped; engine accepts the
  richer selectors already).
- Drawings: `.step+` on-disk persistence of drawings (annotations survive in-session + round-trip
  through `drawing_get`).
- Configurations: per-config mate values / component transforms.
- Instant3D: non-Z extrude handle direction; variable-fillet `radius2` handle (value editable via PM).
- Selector drift on dimension-driven face moves (the existing geometric-resolution limitation,
  `selectors.md §2`) — not specific to any new feature.

## Process notes (for future sessions)

- The ribbon has **two** sources that must stay in sync: `ui/ribbon_def.cpp kCmds[]` (commands) and
  `ui/widgets/ribbon.cpp kRibbonTabs[]` (the tab strip render list). A new ribbon TAB must be added
  to **both** or it never renders (Sheet Metal was unreachable until this was caught).
- Subagents must **not** run git (one renormalized line endings + churned commit hashes early on —
  content intact, but noisy). The orchestrator commits.
- Run engine tests with `-X utf8` (a `∩` char in `test_datum_methods` fails the default Windows
  charmap console — printing only, not logic).
