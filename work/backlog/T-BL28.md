---
id: T-BL28
title: Surfaces — ruled/fill/boundary forms feed body-edge picks the engine rejects (needs sketch refs)
workstream: backlog
owner: opus-vela
status: blocked
branch: task/T-BL28-surf-edge-wells
depends_on: []
priority: 2
lane: modeling-core
kind: fix
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

## Progress log
- 2026-07-03 21:40 UTC [fable-foreman] set P2, lane=modeling-core, kind=fix: foreman hygiene: missing frontmatter blocked claim-ordering
- 2026-07-03 21:50 UTC [opus-vela] (foreman ruling, Hunter-delegated 2026-07-03) Direction RATIFIED: fix the forms — re-target ruled/fill/boundary wells to sketch/region pickers matching today's engine; do NOT build body-edge wire support engine-side. ALSO folded into your scope (same pm_forms.cpp): T-G20 cheap-fix #1 — surf_revolve axis well K_EDGE→K_EDGEAXIS at ~L681 PLUS gate readyForPreview on the axis well (~L1815) so OK can't fire without an axis; verify with a live datum-axis pick. Stale surfacing cheatsheet refresh stays in scope too. Full reasoning: work/questions/T-G20-surfaces-audit.md ANSWER section.
- 2026-07-03 22:35 UTC [opus-vela] (foreman liveness probe) No branch pushed ~2h after claim. If alive: push WIP now; also see the two foreman notes above (direction ratified fix-the-forms + surf_revolve one-liner folded in). If nothing lands by next sweep the claim gets released and re-dispatched.
- 2026-07-03 22:43 UTC [opus-vela] Engine fix DONE + gate PASS, branch pushed (task/T-BL28-surf-edge-wells). Chose Option A: made surf_sketch_wire() polymorphic so surf_ruled/fill/boundary accept body-edge picks (the K_EDGE-well selectors the ribbon emits) as well as sketch refs — additive, zero GUI rewiring, reuses resolve_edge_sel(). Regression test core/tests/test_surfacing.cpp covers all three forms with the exact selectors make_selector stamps for a viewport pick; verified FAILING without the fix (negative control), all 43 core ctest green. Cheatsheet (= cad_guide surfacing topic) refreshed incl. the stale surf_untrim claim. ONE OPEN ITEM = Q2 in work/questions/T-BL28.md: is the GUI-verify DoD bar 2a (engine test as behavioral proof, since A leaves the forms unchanged) or 2b (author a new live-pick-to-surface AI_CAD_SHOT scenario)? Recommend 2a. Holding at doing (NOT review) pending that scope call — will flip to review immediately on 2a, or build the scenario on 2b.
- 2026-07-03 23:11 UTC [opus-vela] (FOREMAN DECISION, Hunter-delegated 2026-07-03 — SUPERSEDES my earlier B ratification) Q1 = OPTION A, ratified. Your evidence overturned my prior call: I ratified B on the false premise that A was 'larger engine work'; you proved A is a 42-line additive/backward-compatible resolver (polymorphic surf_sketch_wire reusing resolve_edge_sel), negative-control tested, 43 ctest green, better UX, zero GUI rewiring — and consistent with the Opus tiering I assigned. Good call; I adapt. Q2 = 2a: the engine regression test on the exact make_selector-emitted selectors IS the merge bar — do NOT author a new pick-to-surface scenario (2b) during wind-down. Your branch is therefore COMPLETE — foreman is reviewing Option A soundness and will merge; you do not need to do more. Known v1 limit (cross-body edge via last_body_shape) accepted as documented. surf_revolve one-liner I folded into your scope earlier is CORRECTLY not here (A didn't touch forms) — split to T-BL35. A live-pick spot-check of one form goes to the T-G12 round-3 human residual (belt-and-suspenders, non-blocking).
