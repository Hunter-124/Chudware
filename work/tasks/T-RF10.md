---
id: T-RF10
title: RF wave-2 GUI feedback batch — 15-item gui-session list + OVERHAUL_SPEC pendings (node R2a)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF10-gui-feedback-batch
depends_on: [T-RF5, T-RF6]
lane: app-gui
kind: fix
priority: 3
---

## Context

docs/plans/2026-07-16-gui-session.md holds a 15-item user-feedback batch (broken ribbon
icons/tab artifacts, PM UX polish — collapsed sections/padding, washer extrude bug, RMB
context-menu rework, zoom-to-fit timing, imperial-aware defaults) and
docs/GUI_OVERHAUL_SPEC.md has pending [~] items (menu removal, chip styling). Desktop ==
web by construction (same host/src/ui compiled to WASM), so every fix lands on both.

## Scope

Execute the 15-item list + pending spec items. Split at dispatch into 2–3 sub-nodes with
disjoint files (ribbon/icons · PropertyManager · viewport/menus); assets/icons/ atlas
merges go through the integrator. Each item verified with an AI_CAD_SHOT scenario,
EYEBALL the PNGs; derived-asset honesty rule applies to any icon work.

## Definition of done

- [ ] Every gui-session item checked off in the doc (or spun out with a reason).
- [ ] OVERHAUL_SPEC [~] items resolved.
- [ ] Shot evidence per item; gate.sh green; REFACTOR_LOG handoff.
