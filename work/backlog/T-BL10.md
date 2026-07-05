---
id: T-BL10
title: SUPERSEDED by workstream R — PDM-lite (git-backed revisioning)
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL10-pdm-lite
depends_on: []
lane: plm-projects
kind: new
---

## Context

Git-backed revision history and release states for .step+ documents; the DAG checkpoint store already provides most of the plumbing.

**SUPERSEDED (2026-07-03): absorbed into Pillar 19 / workstream R —
[docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md).
Named revisions/release states → T-R2/T-R4; eval_compare diff view → T-R3/T-R5.
(Note: the design deliberately does NOT reuse the DAG checkpoint store — see plan
§3.5.) Do not claim this file; claim the T-R tasks.**

**CONTRACT FROZEN (T-R1, 2026-07-03):** the git-backed revisioning + release-state
schema is frozen in [contracts/plm.md](../../contracts/plm.md) (`aicad.plm/1`).
This file's DoD maps to: named revisions/release states → rev letters + fixed
lifecycle `In work → Released → Superseded` (contracts/plm.md §4, §5, delivered by
T-R4); diff view between revisions via `eval_compare` → `plm_compare` reusing
`doc_diff` + `eval_compare` (contracts/plm.md §7, delivered by T-R3/T-R5). Git
binding chosen in [docs/PLM_OSS_SURVEY.md](../../docs/PLM_OSS_SURVEY.md): dulwich
(Apache-2.0 arm), NOT the DAG checkpoint store.

## Definition of done

- [ ] Named revisions + release states on docs
- [ ] Diff view between revisions via eval_compare
