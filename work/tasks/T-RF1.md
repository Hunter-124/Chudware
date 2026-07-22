---
id: T-RF1
title: RF wave-0 docs + board reconciliation (node R0a)
workstream: RF
owner: integrator
status: todo
branch: task/T-RF1-docs-reconcile
depends_on: []
lane: platform-infra
kind: overhaul
priority: 2
---

## Context

Kickoff node of the consolidation refactor (monolithic DAG plan, 2026-07-22). Docs lag
reality after the ui/imgui reversal: top AGENTS.md still says ~52k LOC for host/src/ui
(actual ~110k) and calls ui/viewport "shared"; WORKFLOW.md §3c still declares ui/imgui the
sole GUI target; ARCHITECTURE.md references the non-existent host/DEPRECATED.md and the
retired ui/imgui; docs/gui_parity_gaps.md is written against the deleted ui/imgui;
T-G1–T-G25 board tasks target the retired shell. The old cursor plan
(cad_modular_refactor_+_gui_abstraction) marks reversed nodes "completed".

## Scope

Fix top AGENTS.md (LOC figure, ui/viewport description, dead-doc links), WORKFLOW.md §3c
(host/ is the sole GUI; ui/imgui retired), ARCHITECTURE.md components section,
README pointer check, mark docs/gui_parity_gaps.md superseded (host/ is now the keeper, so
its premise is inverted), retire stale T-G* tasks (mark superseded/done per actual state),
append the REFACTOR_LOG.md supersession note for the old cursor plan + this DAG's start.

## Definition of done

- [x] No doc references ui/imgui, ui/abstract, host/DEPRECATED.md,
      docs/PYTHON_CUTOVER_STATUS.md, or docs/CPP_MIGRATION.md as live.
- [ ] AGENTS.md states ~110k LOC host/src/ui and the real ui/viewport status.
- [ ] T-G* board statuses reflect reality; `work.py validate` clean.
- [ ] REFACTOR_LOG.md paragraph appended; `graphify update .` run.
