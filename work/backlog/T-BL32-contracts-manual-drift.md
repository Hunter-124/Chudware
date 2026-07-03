---
id: T-BL32
title: docs/manual/contracts.md drift — missing license_debt.md row + document the new CAM scoring keys
workstream: backlog
owner: unassigned
status: todo
priority: 4
lane: platform-infra
kind: fix
depends_on: []
branch: task/T-BL32-contracts-manual-drift
---

## Context

Two small documentation drifts flagged during T-BL23 (2026-07-03), confirmed real at
review:

1. `docs/manual/contracts.md` is missing a row for `contracts/license_debt.md` (the
   license debt register introduced 2026-07-03 — see product-priority directive).
2. The verifier battery now scores CAM via `machined_fraction` (0..1 scalar) with a
   gouge pre-gate (any `gouges > 0` → score 0.0; `gouge_tol` is 0.05 mm in
   `core/cam/stock_sim.h`) — landed in T-BL23, `services/verify/battery.py::_extract_score`.
   Whatever manual/contract page documents verify-battery scoring keys should reflect it.

## Definition of done

- [ ] `docs/manual/contracts.md` lists `contracts/license_debt.md` with a one-line
      description consistent with the other rows.
- [ ] CAM scoring keys (machined_fraction + gouge pre-gate + tolerance) documented where
      the battery's other scoring keys are documented; wording matches the implemented
      semantics in `services/verify/battery.py` (do not promise more than the code does).
- [ ] No code changes. `graphify update .` before review (docs are indexed).

## Driver notes

Pure docs task, minutes not hours. graphify query "verify battery scoring documentation"
to find where scoring keys are documented before grepping. Sonnet/Haiku tier.

## Progress log
