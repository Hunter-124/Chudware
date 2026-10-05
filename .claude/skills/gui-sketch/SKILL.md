---
name: gui-sketch
description: Chudware sketcher UI specialist — the 2D constraint-solver sketch mode (tools, gestures, snapping, relations, smart dimensions, trim/offset/patterns) and the 3D free-space sketch mode. Use for any task about sketching UX, sketch entities, constraints/relations display, dimension labels, or sketch tool behavior. Workers dispatched by gui-orchestrator read this file first instead of searching the repo.
---

# GUI Sketch Specialist

Scope: the in-canvas sketching experience, 2D and 3D. The ribbon buttons that *enter* sketch
tools are `gui-ribbon-commands` (table rows in `ribbon_def.cpp`); this skill owns what happens
once the tool is active. Shared-file ownership, waves and verification: `gui-orchestrator`.

## File map

| File | Role |
|---|---|
| `host/src/ui/sketch_mode.cpp/.h` (**largest UI module**) | Entire 2D sketcher: tool state machine, gestures, entity drawing, constraint/relation glyphs, smart-dimension labeling, trim/extend/offset/convert/mirror/pattern tools |
| `host/src/ui/sketch3d_mode.cpp/.h` | 3D sketcher: free-space curves in world coordinates |
| `contracts/interaction.md` | The UI gesture language (click/drag/hover semantics) — follow it |
| `contracts/sketch3d_gui_contract.md` | Frozen 3D-sketcher UX spec |

Per-doc sketcher state hangs off `AppState::DocTab` (`SketchMode`, `Sketch3DMode` members) in
`host/src/core/app_state.h`.

## How the sketcher relates to the engine

The sketcher UI edits sketch entities/constraints and sends ops to `chudware-hostd`, where
libchudware owns the constraint solver; solved geometry comes back via the per-frame event drain
(`host/src/core/engine_events.cpp`). Touching endpoints are auto-coincident on every sketch
edit — profiles close on their own; don't add redundant coincident constraints in UI flows.

## Working in sketch_mode.cpp

Navigate by tool: each sketch tool (line, arc variants, slot variants, spline
variants, …) has a state-machine branch keyed by the active command id from the ribbon table.
Tool names/flyout structure mirror the icon inventory in `contracts/icons.md` (Sketch tab +
Sketch flyouts sections) — that list is the authoritative set of tools that should exist.
Before editing, grep *within this file* for the tool's cmd_id or label rather than repo-wide.

## Conventions & gotchas

- Colors via `ui_theme` only (`scripts/gui_lint.py --rule color` gates); dimension-label and
  glyph styling must be DPI-aware — scale via `host/src/ui/metrics.h`, never hardcoded px
  (`--rule scale`).
- Gestures must match `contracts/interaction.md`; if a new tool needs a new gesture, that
  contract gets an appended entry (integrator-owned — flag it to the orchestrator).
- Scenarios `CHUDWARE_SHOT=sketch` and `CHUDWARE_SHOT=sketch3d` (`host/src/ui/scenarios.cpp`)
  run scripted sketch sessions headlessly through the real engine.
- Inactive sketches ghost in the viewport (`SketchMode::drawGhosts`) unless a BUILT feature
  consumes them. Consumption has one definition, `core::consumedSketches`
  (`host/src/core/sketch_consumers.h`), shared with the FeatureManager's sketch nesting:
  `refs.profile` as a list/single id/region selectors, `*sketch*` keys, sweep `path`/`guides`,
  loft `sections`; never a pattern `features` list. Ghosts draw with no depth test, so a missed
  consumer makes the finished solid look see-through.
  Ghost frames come from engine `doc_sketch_overview`; the engine's
  `resolve_sketch_plane_doc` applies the sketch's `params.offset` exactly as
  `compile_document` does, so never add the offset again in the host.
