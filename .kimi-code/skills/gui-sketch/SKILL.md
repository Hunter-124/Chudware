---
name: gui-sketch
description: Chudware sketcher UI specialist — the 2D constraint-solver sketch mode (tools, gestures, snapping, relations, smart dimensions, trim/offset/patterns) and the 3D free-space sketch mode. Use for any task about sketching UX, sketch entities, constraints/relations display, dimension labels, or sketch tool behavior. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Sketch Specialist

Scope: the in-canvas sketching experience, 2D and 3D. The ribbon buttons that *enter* sketch
tools are `gui-ribbon-commands` (table rows in `ribbon_def.cpp`); this skill owns what happens
once the tool is active.

## File map

| File | Role |
|---|---|
| `host/src/ui/sketch_mode.cpp/.h` (~8.9k LOC — **largest UI module**) | Entire 2D sketcher: tool state machine, gestures, entity drawing, constraint/relation glyphs, smart-dimension labeling, trim/extend/offset/convert/mirror/pattern tools |
| `host/src/ui/sketch3d_mode.cpp/.h` | 3D sketcher: free-space curves in world coordinates |
| `contracts/interaction.md` | The UI gesture language (click/drag/hover semantics) — follow it |
| `contracts/sketch3d_gui_contract.md` | Frozen 3D-sketcher UX spec |

Per-doc sketcher state hangs off `AppState::DocTab` (`SketchMode`, `Sketch3DMode` members) in
`host/src/core/app_state.h`.

## How the sketcher relates to the engine

The sketcher UI edits sketch entities/constraints and sends ops to the Python kernel (the
constraint solver lives engine-side); solved geometry comes back via the per-frame event drain
(`host/src/core/engine_events.cpp`). Touching endpoints are auto-coincident on every sketch
edit — profiles close on their own; don't add redundant coincident constraints in UI flows.

## Working in sketch_mode.cpp

At 8.9k lines, navigate by tool: each sketch tool (line, arc variants, slot variants, spline
variants, …) has a state-machine branch keyed by the active command id from the ribbon table.
Tool names/flyout structure mirror the icon inventory in `contracts/icons.md` (Sketch tab +
Sketch flyouts sections) — that list is the authoritative set of tools that should exist.
Before editing, grep *within this file* for the tool's cmd_id or label rather than repo-wide.

## Conventions & gotchas

- Colors via `ui_theme` only (`scripts/gui_lint.py --rule theme` gates); dimension-label and
  glyph styling must be DPI-aware — scale via `host/src/ui/metrics.h`, never hardcoded px.
- Gestures must match `contracts/interaction.md`; if a new tool needs a new gesture, that
  contract gets an appended entry (integrator-owned — flag it to the orchestrator).
- Scenario smoke tests exist: `CHUDWARE_SHOT=sketch` and `CHUDWARE_SHOT=sketch3d`
  (`host/src/ui/scenarios.cpp`) run scripted sketch sessions headlessly.

## Build & verify

```bash
./build.sh
CHUDWARE_SHOT=sketch ./host/build-linux/bin/Chudware --ui-screenshot scratch/sketch_smoke.png
```
Smoke = "doesn't crash", not visual judgment. Never run git. Return files changed + a
one-line user check, e.g. "enter a sketch, pick Slot > 3-point arc slot — rubber-band preview
follows cursor and dimension appears on commit."
