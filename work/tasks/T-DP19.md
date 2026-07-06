---
id: T-DP19
title: Gym real-run interpreter fix — let the tuning loop produce real per-class gradients
workstream: DP
owner: opus-dp19
status: review
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

- [x] Gym real-run missions execute past PLAN (no langgraph.checkpoint.sqlite error);
      the DAG now executes ALL phases as nodes (INTAKE→SPECIFY→ORGANIZE→PLAN→BUILD→
      VERIFY→DELIVER). *(Caveat: the PHASE MACHINE gates at SPECIFY because qwen3-4b
      doesn't record the acceptance criteria the specify overseer-audit needs — a
      model-capability signal the loop MEASURES, not an infra failure. The BUILD node
      itself runs. See Results.)*
- [x] tuning.py re-run produces real per-class win rates written to
      reports/tuning_results.json; per_class_winners feeds the router as tier-default
      fallback (tied). Honest 0.00 from qwen3-4b's own merits, zero cloud errors.
- [ ] scripts/gate.sh PASS; graphify update.  *(run at commit time)*

## Results (2026-07-06, opus-dp19)

**Root cause was two-layered, both fixed:**

1. **Interpreter (the named bug).** The gym runs run_mission IN-PROCESS (not a
   subprocess — the work order's framing was off). run_mission's DAG phase lazily
   imports `services.dag.executor`, whose top-level `from langgraph.checkpoint.sqlite
   import SqliteSaver` raises under system python3 → mission dies, labeled PLAN, every
   cell ties 0.00. Fix: `ensure_capable_interpreter()` re-exec guard at each gym
   entrypoint (tuning/cli/run_dp2_gym) re-execs into the mcp-venv (`AI_CAD_PYTHON` →
   mcp-venv, loop-sentinel, never hard-fails). `dag_deps` added to the ExecProbe so
   `can_run_real` is honest when the dep is absent. **Gotcha caught:** a venv python is
   a symlink to its base, so `os.path.realpath` collapsed the mcp-venv onto
   /usr/bin/python3 and skipped it — use `abspath`, not `realpath`.

2. **Provider routing (found while verifying).** With the import fixed, real cells
   still 0.00'd — a `local-24k` cell was routing to the ambient config.json provider
   (Gemini, HTTP 429/503), NOT the qwen3-4b the tier names. Fix: `_provider_for_tier()`
   — a `local-*` cell resolves the bundled qwen3-4b (llamacpp provider, GBNF), threaded
   into `_run_mission_real(challenge, run_id, tier)`. Explicit `AI_CAD_PROVIDER` still
   wins; non-local tiers unchanged.

**Verified honest run** (C1 × {eco,maximum} × seed1, whole pipeline on qwen3-4b via a
scratch config monkeypatch — see HANDOFF): mode=real, **0 cloud provider errors**,
per_class_winners `multi_feature_part|local` → eco 0.00 / maximum 0.00 (tied →
tier-default fallback). The overseer audit node genuinely ran on qwen3-4b (5 turns,
real ledger_get calls); grading fails on merits (AC1 "no bore dimension recorded").

## HANDOFF — follow-ups (NOT in T-DP19 scope)

- **[needs advisor/human design — NOT a simple patch] Local-tier missions route
  internal nodes to the ambient cloud provider.** `services/dag/node_runner.py:152`
  resolves `config.get_provider()` (no-arg) → config.json default, so a Python
  `loop.run_mission` node (overseer-audit/build/deliver) runs on the AMBIENT provider
  (Gemini) even for a `local-24k` cell. **Do NOT "just thread the mission provider"** —
  advisor.md §14 (routing.yaml) mandates PER-ROLE routing (overseer→strongest,
  sketcher→cheap_local) and says "code must not special-case any role's model choice",
  so forcing one provider onto all nodes violates the contract. The real fix is a §14
  routing override that pins local-tier runs to local per-role models (routing.yaml
  layer, not the node_runner call site) — a Fable/human design call. Scope note: the
  PRODUCTION app executes missions through the C++ core (core/agent/run.cpp::run_mission),
  not this Python path, which is bench/gym-exercised — so the impact today is "gym real
  runs need the config override," already handled. Tonight's honest run used a
  `config.load_config` monkeypatch (operator plumbing, no code / no edit to config.json).
  Repro/evidence: scratchpad/dp19_local_tuning.py.
- **Fuller C-suite gradient.** Recorded result covers C1 only (minimal, timeboxed).
  Re-run the same driver with `challenge_ids=["C1","C1-DP2",...]` for the full suite;
  expect more honest 0.00s at the specify wall until scaffolding/model improves.
- **specify wall is what the loop measures** — do NOT "fix" it here (advisor ruling).

## Notes

This unblocks the measured "best workflow per task-class" signal that auto-mode
(T-DP10) targets — the tokenmaxx tuning payoff. Infra is ready; this is the env fix
plus a real re-run.
