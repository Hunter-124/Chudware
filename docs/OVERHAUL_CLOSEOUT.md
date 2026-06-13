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
- Broad `engine + agent`: **429 passed**. The only failures are **pre-existing/environmental**,
  identical to the pre-overhaul baseline and outside any code this overhaul touched:
  `engine/sim/test_fea.py` (3, gmsh `WindowsPath`), `engine/sim/test_lbm.py` (2 errors),
  `agent/test_compaction.py` (2, offline-mode config). **The overhaul introduced zero regressions.**
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
