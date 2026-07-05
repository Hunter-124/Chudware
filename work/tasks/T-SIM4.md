---
id: T-SIM4
title: Solver validation depth - fatigue S-N anchor, FEA live bridge integration test, sim_audit extension
workstream: SIM
owner: unassigned
status: todo
branch: task/T-SIM4-solver-validation
depends_on: [T-SIM1]
lane: simulation
kind: fix
priority: 3
---

## Context

T-V1 audit left validation debt on solvers that are REAL but never live-checked:
- FEA fatigue (core/sim/fea_fatigue.cpp, 14K) has no hand-calc anchor (no S-N reference).
- FEA static/modal have no bridge-exposed live integration test in CI (the audit could
  not exercise them without the full MCP server).
- scripts/sim_audit.py executed 8 checks, skipped 23 — the skip list should shrink.

## Definition of done

- [ ] fatigue_anchor() in services/agent/advisor/sim_advisor.py (Goodman/Soderberg S-N
      reference case, cited source for the S-N data) + pytest coverage.
- [ ] Live FEA integration test: cantilever static + modal through the real bridge
      (engine doc-load contract: doc_new + eq_set + add_feature, then sim run), asserting
      against beam-theory anchors (40.8 Hz modal anchor already documented). Marked/gated
      so it runs where the core .so is present, honest skip elsewhere (no all-skip trap —
      must run in the local gate).
- [ ] scripts/sim_audit.py re-run: skip count reduced, updated verdict table appended to
      work/audits/SIM_AUDIT.md with date.
- [ ] Thermal anchor check added to sim_audit.py once T-SIM1 is merged (dep).
- [ ] scripts/gate.sh green (issue #16 known failures excepted); `graphify update .` run.

## Driver notes

- Python test venv: `~/.local/share/AI-CAD/mcp-venv/bin/python`.
- graphify rule: `graphify query "<question>"` before raw-file reading.
- Branch task/T-SIM4-solver-validation, worktree ../CAD-T-SIM4. Stop at status: review.
- OpenFOAM integration is NOT this task (see T-SIM5, deferred).
