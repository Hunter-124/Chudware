---
id: T-BL49
title: AI fitment & interface-alignment checker — interface_check tool for mating components (hole patterns, coaxiality, contact planes, fastener clearance)
workstream: backlog
owner: opus-gill
status: doing
priority: 2
lane: ai-copilot
kind: new
depends_on: []
branch: task/T-BL49-interface-fitment-checker
---

## Context

Cut 2026-07-04 by fable-helm from a live Hunter directive: "make sure that the ai has the
tooling/helpers it needs to help it check layouts for proper fitments and properly
alignment of component interfaces, ensuring proper layout plans the first time."

Gap analysis (verified 2026-07-04): `check_interference` catches hard clash/clearance
violations; `fit_report`/`confirm_dimensions` belong to the reverse-engineering intake
ledger (measured physical parts). NOTHING today checks design-time **interface
alignment** between components — the class of error where an assembly passes
interference (nothing overlaps) but is still wrong: bolt holes that don't line up,
mating bores that aren't coaxial, contact faces that aren't flush, a screw that can't
pass through the stack.

## Definition of done

- [ ] `interface_check` MCP tool: given two components/bodies in an assembly doc (or
      auto-detecting candidate mating pairs from mates + proximity), reports per
      interface:
      - **Hole-pattern alignment:** matching hole sets across the interface (position
        deltas, diameter compatibility) — flag misaligned or missing counterparts with
        numeric deltas, not just pass/fail.
      - **Coaxiality:** mating cylindrical features (bore/boss, shaft/bore) axis
        misalignment (distance + angle).
      - **Contact-plane check:** intended mating faces coplanar/parallel within
        tolerance, gap/overlap value reported.
      - **Fastener pass-through:** for a hole stack, whether a nominal fastener passes
        (clearance-class check — reuse the existing thread/hardware spec helpers, e.g.
        research_thread_spec / hardware tables; do not invent new fit tables).
- [ ] Findings are ACTIONABLE for an agent: each finding names the components, the
      features (in the plain-words ref style the engine already uses), the numeric
      error, and the fix direction (e.g. "hole pattern on plate_b offset 1.5mm in +X
      relative to bracket_a").
- [ ] Wired into the review loop: `design_review`/verify battery can run interface_check
      as a check; cad_guide('review') and the assembly guidance mention it (MCP
      freshness, WORKFLOW.md §6) so the copilot actually reaches for it during layout.
- [ ] Regression: a worked two-component example (e.g. bracket + plate with a 4-hole
      pattern) — deliberately misaligned version FLAGGED with correct deltas, corrected
      version PASSES. Real engine geometry, asserted in a test.
- [ ] Honest scope note: what v1 does NOT infer (e.g. intended-but-unmated interfaces
      beyond proximity heuristics) documented in the tool description.

## Driver notes

- **Tier: OPUS** — real geometry-kernel queries (feature/face classification, axis
  extraction, pattern matching across bodies) on OCCT via the existing engine query
  layer.
- graphify-first: `graphify query "assembly mates solve_mates components"`,
  `graphify query "query_geometry adjacency face classification"`,
  `graphify query "check_interference implementation"` before raw reads.
- Reuse, don't fork: `check_interference` (clash), `query_geometry`/`query_adjacency`
  (feature interrogation), `solve_mates` (mate graph → which pairs are intended to
  mate), hole-extraction machinery from CAM (T-M5 OCCT hole extraction — position,
  axis, diameter per hole) is likely the fastest path to hole-pattern data.
- COORDINATION: opus-dune (T-BL16 motion sim) is in the mate-solver area concurrently;
  sim MCP registrations in services/mcp/server.py are append-only, rebase early.
- Related: T-BL08 (tolerance stack-up, in flight same wave) covers the tolerance math
  side; T-BL50 covers the plan-first workflow that calls this tool. Keep the tool's
  JSON output stable/documented — T-BL50's coverage gate consumes it.

## Progress log
