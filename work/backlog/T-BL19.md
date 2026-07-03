---
id: T-BL19
title: Modeling-op combination battery - engine-level AI testing of feature tools and sequences
workstream: backlog
owner: unassigned
status: todo
branch: task/T-BL19-modeling-battery
depends_on: []
---

## Context

**Cut 2026-07-03 (Erik-directed, T-G12 round-1 process note.)** Erik cannot manually
test every modeling feature/tool and combination — AI testing has to carry that
coverage. Ratified scope for v1: **engine-level combinations** (no GUI) — a headless
battery driving the MCP/engine API through every modeling op and op-sequence
combinations, checking that the result is sane. A GUI click-through battery is a
separate follow-up once the round-1 style/icon work stabilizes the UI.

Battery shape:
- Single-op pass: every modeling op (sketch+extrude/revolve/sweep/loft, primitives,
  fillet/chamfer/shell/draft/rib, holes, patterns/mirror, booleans, curves, surface
  ops, sheet-metal ops) executed on a suitable seed part.
- Pairwise/sequence pass: op A then op B over a curated compatibility matrix (e.g.
  shell-after-fillet, pattern-of-boolean, cut-through-pattern), plus a small set of
  longer realistic recipes.
- Checks per case: rebuild success (CoreError vs feature_status), manifoldness,
  mass-property sanity (volume decreases on cut, increases on boss), and
  check_feature_overlap where applicable.

## Definition of done

- [ ] Battery script/harness checked in, one-command runnable, emitting a JSON + MD
      report (reuse the T-BL01 V&V harness report conventions)
- [ ] Single-op coverage: every registered modeling op executed at least once (no
      all-skip — every case really runs; skips are failures)
- [ ] Pairwise/sequence matrix defined and run; failures triaged into GitHub issues
      (claimed via the issue registry, not silently fixed)
- [ ] Honest coverage statement: which ops/combos are NOT covered and why
- [ ] Wired as an opt-in gate stage (env-gated like the T-BL01 battery) so it can run
      nightly without blocking normal gates

## AI hints

- engine-doc-load-verify-contract memory: doc_new + eq_set + add_feature loading and
  rebuild-failure detection — the core of each case.
- T-BL01's V&V harness (merged) is the report/tolerance-tier pattern to copy.
- fleet-recovery-patterns memory: the all-skip test trap — assert the battery FAILS
  if every case skips.
