---
id: T-BL49
title: AI fitment & interface-alignment checker — interface_check tool for mating components (hole patterns, coaxiality, contact planes, fastener clearance)
workstream: backlog
owner: opus-gill
status: done
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

## JSON contract (for T-BL50 — STABLE, version 1)

`interface_check(doc_id?, components?=[a,b,...], fastener?="M6", proximity_mm?=2.0)` returns
JSON (full field docs at the top of `services/mcp/interface_check_tools.py`):

```
{
  "ok": bool,                # True iff NO finding has severity "fail"
  "doc_id": str,
  "tool": "interface_check", "version": 1,
  "components": [{"id","name","bbox"}],
  "pairs": [                 # one per examined component-pair interface
    {"a","b","a_name","b_name",
     "checks": {
       "hole_pattern":         {"status","matched":[],"misaligned":[{pos_delta_mm,offset_vec_mm,dia_delta_mm,fix}],"unmatched_a":[],"unmatched_b":[],"summary"},
       "coaxiality":           {"status","pairs":[{a_feature,b_feature,axis_offset_mm,axis_angle_deg,status,fix}],"summary"},
       "contact_plane":        {"status","gap_mm","parallel_angle_deg","a_feature","b_feature","fix","summary"},
       "fastener_passthrough": {"status","fastener","nominal_mm","stacks":[{nominal_mm,min_hole_dia_mm,clearance_mm,passes,fix}],"summary"}
     }}
  ],
  "findings": [              # flat, agent-actionable; the payload design_review/verify narrate
    {"severity":"fail"|"warn","component_a","component_b","check","feature","error_mm","detail","fix"}
  ],
  "summary": str, "scope_note": str
}
```

T-BL50's coverage gate should read `ok` + `findings[].severity=="fail"` (advisory `warn`
findings do not fail the gate). `check` ∈ {hole_pattern, coaxiality, contact_plane,
fastener_passthrough}. Field names/`version` are frozen; additive-only changes bump `version`.

## Progress log

- 2026-07-04 opus-gill: shipped `interface_check` (services/mcp/interface_check_tools.py):
  4 sub-checks (hole-pattern registration, bore coaxiality, contact-plane flushness, fastener
  pass-through). Reuses `doc_query` (per-body faces via a new 1-based `body_id` selector added
  to core/doc/session.cpp, mirroring eval_measure/eval_mass — default preserves bodies[-1]),
  `asm_world_bodies`, and `research_thread_spec` (no new fit tables). Wired into the review
  loop: `verify` battery (services/verify/battery.py), `design_review`
  (services/mcp/design_review_tools.py), server.py registration, and cad_guide('review').
  Regression: services/mcp/test_interface_check.py — misaligned 4-hole bracket+plate FLAGGED
  with pos_delta≈1.5mm in +X; corrected (dx=0) PASSES; M8 fastener fails / M5 passes; plus a
  doc_query body_id per-body + out-of-range unit test. Built + tested against a worktree-local
  core build (system OCCT); main checkout left clean.
- 2026-07-04 opus-gill (resume after crash): finished the interrupted checkpoint — an
  incomplete `_holes_from_faces` → `_cylinders_from_faces` refactor had left a dangling call
  + mismatched `_check_coaxiality` signature (would NameError at runtime). Completed the split
  (per-body cylinders cached once as (bores, bosses); hole_pattern + fastener run on bores,
  coaxiality now matches boss/shaft-in-bore both directions) and re-wired the callsites.
  Confirmed the body_id doc_query change is on-branch (commit e98b4b90) and unit-tested; stray
  main-checkout edit was NOT needed. Rebased on origin/master (clean). All 8 tests PASS against
  the real engine (core/build-linux/bin/libchudware.so) — verified executed, not skipped.
- **INTEGRATOR / T-BL50 join point:** once `services/mcp/layout_plan_tools.py` lands, add
  `"interface_check"` to its `_INTERFACE_VERIFIERS` map so the layout coverage gate consumes
  this tool's output. Gate reads top-level `ok` + any `findings[].severity=="fail"` (advisory
  `warn` does not fail the gate). JSON contract above is version 1, frozen (additive-only bumps
  `version`). NOTE: the ctypes bridge (`services/mcp/core_bridge.py`) resolves the .so root as
  `parents[1]` = the repo's `services/` dir, so its default search paths point under
  `services/core/build-*` which do not exist in a normal checkout; set
  `CHUDWARE_LIBCHUDWARE=<repo>/core/build-linux/bin/libchudware.so` (or stage the .so) to run the
  engine-backed tests — otherwise `cb.available()` is False and the suite silently all-skips.
