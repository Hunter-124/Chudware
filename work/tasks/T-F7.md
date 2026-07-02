---
id: T-F7
title: Freeze narration contract + describe_toolpaths + describe_results
workstream: F
owner: unassigned
status: todo
branch: task/T-F7-narration-core
day: 6
depends_on: []
---

## Context

Pillar 11 root: every domain state must have a deterministic `describe_*` so any model,
vision or not, can read the design. Biggest current gaps: CAM (raw JSON only) and
FEA/CFD (named scalars, no narrative). READ `docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 11.
This is also the prerequisite for translator training data (T-F10) and for report
content in the deliverables suite (T-F15).

## Files / areas touched

- `contracts/narration.md` — new: shared narration style guide, sid-stable naming,
  every number traceable to a runnable query
- `services/mcp/gsl_tools.py` (or sibling module) — `describe_toolpaths(job)`,
  `describe_results(study)`
- `services/mcp/server.py` — registrations; `cad_guide` topics

## Steps

1. Freeze `contracts/narration.md` mirroring `describe_part`'s conventions.
2. `describe_toolpaths`: per-op narrative (strategy, tool, stepdown/over, engagement,
   cycle time, remaining stock, collisions/gouges) + G-code digest (bounds, feeds
   actually emitted, tool changes).
3. `describe_results`: where max stress is (named faces via sid join), margin vs
   material yield, mode shapes in words, convergence caveats.
4. Tests in the house pattern of `test_gsl_tools.py` — deterministic fixtures, exact
   slot assertions.

## Definition of done

- [ ] Contract frozen; both describers registered with cad_guide topics
- [ ] Every numeral in output traceable to a tool query; tests green
- [ ] `bash scripts/gate.sh`; `graphify update .`

## AI hints

- `graphify query "gsl describe_part narration"` — mirror the mature GSL layer's shape.
- CAM data source: `cam_job_tree`/`cam_setup_sheet`/`cam_extract` already return the
  raw JSON these narrate.
