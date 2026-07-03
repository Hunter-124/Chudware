---
id: T-INF2
title: Retire the last pre-existing gate reds (check.py audits) - full-green gate
workstream: V
owner: unassigned
status: todo
branch: task/T-INF2-checkpy-green
day: 5
priority: 3
depends_on: []
---

## Context

Every stage of scripts/gate.sh is green EXCEPT check.py's two FAIL-RECORDED audits,
red since before the sprint: feature_surface_audit (ribbon -> handlers) and
parity_audit (spec not found at docs/SW_PARITY_SPEC.md). Every driver has been
trained to shrug at them all night — that normalization eventually hides real drift.

## Driver notes (foreman)

- parity_audit: the spec file is MISSING — find where SW_PARITY_SPEC.md went (git log
  --all -- '*SW_PARITY_SPEC*'), restore/regenerate or retarget the audit to the
  current parity source of truth (tools/gui_parity/ now exists and PROVES parity —
  maybe the audit should point there; record the decision).
- feature_surface_audit: run it, read the actual ribbon->handler gaps, fix the real
  ones / annotate the deliberate ones per the audit's own mechanism. The T-G12
  inventory (scripts/ui_inventory.py) cross-checks 225 cmds — reconcile.
- DoD is a FULL-GREEN gate: `bash scripts/gate.sh` exit 0 with zero FAIL-RECORDED
  lines, honestly (no audit deletions — fix or formally retarget with rationale).

## Definition of done

- [ ] gate.sh RESULT: PASS with no FAIL-RECORDED entries
- [ ] Both audits' fixes/retargets documented with rationale
- [ ] `graphify update .` run
