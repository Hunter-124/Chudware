---
id: T-BL32
title: docs/manual/contracts.md drift — missing license_debt.md row + document the new CAM scoring keys
workstream: backlog
owner: sonnet-osier
status: review
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

- [x] `docs/manual/contracts.md` lists `contracts/license_debt.md` with a one-line
      description consistent with the other rows.
      (Already present at line 18 from a prior master commit — no change needed.)
- [x] CAM scoring keys (machined_fraction + gouge pre-gate + tolerance) documented where
      the battery's other scoring keys are documented; wording matches the implemented
      semantics in `services/verify/battery.py` (do not promise more than the code does).
      (Updated `training/benches/cam/README.md` §Substrate ruling — complete scalar key
      table + gouge pre-gate semantics matching battery.py::_extract_score exactly.)
- [x] No code changes. `graphify update .` before review (docs are indexed).

## Driver notes

Pure docs task, minutes not hours. graphify query "verify battery scoring documentation"
to find where scoring keys are documented before grepping. Sonnet/Haiku tier.

## Progress log

- 2026-07-04 sonnet-osier: DoD #1 — contracts.md license_debt row already present from
  prior master commit (confirmed line 18). No change needed.
- 2026-07-04 sonnet-osier: DoD #2 — updated training/benches/cam/README.md §Substrate
  ruling with full scalar key table + gouge pre-gate semantics matching battery.py exactly.
  Location rationale: this README was the document that listed the old scalar key set and
  explicitly noted machined_fraction/gouges were absent (T-H8 Q1). It's the canonical home
  for battery scoring key documentation for the CAM bench context. No other docs page
  documented the scalar key list.
- 2026-07-04 sonnet-osier: graphify update run, gate.sh passed. status → review.
