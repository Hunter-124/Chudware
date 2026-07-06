---
id: T-DP19
title: Gym real-run interpreter fix — let the tuning loop produce real per-class gradients
workstream: DP
owner: unassigned
status: todo
branch: task/T-DP19-gym-realrun-interpreter
depends_on: [T-DP10]
lane: ml-flywheel
kind: fix
priority: 2
---

## Context

T-DP10 shipped the auto-mode workflow router + the gym tuning loop
(tools/flywheel/bench/gym/tuning.py) — the infrastructure is correct and merged. But
its real gym runs (cycle dp10-tuning-real) all tied at win_rate 0.00 because the
mission execution failed at the PLAN phase with `No module named
'langgraph.checkpoint.sqlite'`.

Root cause (integrator diagnosis 2026-07-05): the module is NOT missing — it is
installed (langgraph-checkpoint-sqlite 3.1.0) and imports fine under the gate venv
(~/.local/share/AI-CAD/mcp-venv/bin/python). services/dag/executor.py imports it and
works in-process. The failure is an ENVIRONMENT/INTERPRETER mismatch in the gym's
real-run path: the mission (run_mission → services/dag executor) is executed by a
python that lacks the package (system python3, or a subprocess that doesn't inherit
the mcp-venv). So real tuning gradients can't be produced yet.

(The declared-dependency half is already fixed: langgraph-checkpoint-sqlite is now in
requirements.txt so a fresh env installs it — T-DP10 follow-up commit.)

## Scope

- Trace the gym real-run mission-execution path (tools/flywheel/bench/gym/runner.py
  run_cell → run_mission / services/dag executor). Determine whether it runs
  in-process or spawns a subprocess, and which interpreter that subprocess uses.
- Make the real-run path use the SAME interpreter that has the deps (propagate
  AI_CAD_PYTHON / sys.executable / the mcp-venv), so run_mission reaches BUILD/VERIFY
  instead of dying at PLAN.
- Re-run the tuning loop (≥2 scaffold configs over the C-suite, qwen3-4b CUDA
  llama-server) and record REAL per-class win rates. Honest numbers whatever they are
  — never fabricate a gradient.

## Definition of done

- [ ] Gym real-run missions execute past PLAN (no langgraph.checkpoint.sqlite error);
      a real cell reaches at least BUILD.
- [ ] tuning.py re-run produces real per-class win rates written to
      reports/tuning_results.json; the router's best_scaffold_for_class consumes them.
- [ ] scripts/gate.sh PASS; graphify update.

## Notes

This unblocks the measured "best workflow per task-class" signal that auto-mode
(T-DP10) targets — the tokenmaxx tuning payoff. Infra is ready; this is the env fix
plus a real re-run.
