---
id: T-BL20
title: Surfaces — ruled/fill/boundary forms feed body-edge picks the engine rejects (needs sketch refs)
workstream: backlog
owner: unassigned
status: todo
branch: task/T-BL20-surf-edge-wells
depends_on: []
---

## Context

Found during **T-G20** (Surfaces tab audit) — full evidence in
`work/questions/T-G20-surfaces-audit.md`.

`surf_ruled` (edge_a/edge_b), `surf_fill` (edges), and `surf_boundary` (dir1/dir2)
resolve their refs through `surf_sketch_wire()` -> `require_sketch()`
(`core/doc/document.cpp` L903-912): they require a **sketch** reference and build a wire
from the whole solved sketch. But the ribbon forms (`host/src/ui/pm_forms.cpp` L706-720)
give these slots **K_EDGE** wells, and the sketch-feature routing in
`property_manager.cpp` is gated to profile/sections (`profileWell`, L1255) and
path/centerline (`routePathFeature`, L1283) only. So these wells can hold **only
body-edge selectors** (owner = a body), which the engine rejects
(`"edge_a -> 'fN' is a solid body, not a sketch"`). Driven & confirmed via MCP: the same
ops build fine from `{kind:region,sketch:...}` refs.

This is a large part of the "surfaces tab is broken" report — the form invites a
body-edge pick that always fails.

## Definition of done

- [ ] Decide the fix direction (design call — see T-G20 Q2):
      (A) teach the engine to build wires from picked **body edges** (SolidWorks-style
      ruled/boundary UX; larger engine work), OR
      (B) re-target the forms to **sketch/region** pickers (matches today's engine).
- [ ] Implement the chosen direction for all three (ruled/fill/boundary)
- [ ] Live-pick GUI verification: build under the host-build lock, drive each form to a
      successful surface, screenshot light+dark, LOOK at every PNG
- [ ] Refresh `services/agent/prompts/shared/cheatsheets/surfacing.md` (also stale re
      surf_untrim — see below) so the agent path matches reality

## AI hints

- The engine handlers are correct; this is a form/selector-layer fix. Start from the
  T-G20 report's "The one real wiring bug" section.
- Cheatsheet stale bit: it claims surf_untrim has "NO compiler" — false since Bug S-1
  (handler at `document.cpp` L5747).
