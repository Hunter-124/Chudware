---
id: T-SIM4
title: Solver validation depth - fatigue S-N anchor, FEA live bridge integration test, sim_audit extension
workstream: SIM
owner: fable-birch
status: doing
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

## Progress log
- 2026-07-05 19:43 UTC [fable-birch] fatigue_anchor (Goodman/Soderberg, Shigley 10th Eq 6-45/6-46 + 6-14..6-16 + 6-8 cited) landed w/ 8 tests; live FEA bridge test landed (cantilever static 0.39% vs Roark, modal 40.86 Hz vs 40.77 Rao anchor 0.22%, runs in gate via occt-build fallback); sim_audit extended 8->16 executed / 23->15 skipped incl live fatigue+thermal cross-checks, SIM_AUDIT.md verdict table appended. Gate running.
