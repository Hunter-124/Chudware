---
id: T-H6
title: Fleet training runner - claim-based job queue, cross-OS profiles, MLflow tracking
workstream: H
owner: unassigned
status: todo
branch: task/T-H6-fleet-runner
day: 13
depends_on: [T-H5]
---

## Context

Pillar 16 (`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §5.4–5.5). Fleet = our own
machines, mixed Windows/Linux, geographically spread; constraint = NO cross-internet
distributed single-run training. Therefore job-level distribution reusing the proven
work.py idiom: training jobs are files in a git-synced `training/jobs/` queue with
atomic claim; `tools/flywheel/runner.py` claims a job, `dvc pull`s the dataset hash,
runs the WHOLE job locally (SFT/DPO/GRPO/eval per job spec), `dvc push`es artifacts,
logs to self-hosted MLflow, posts a work_note. Jobs declare
`requires: {os: any|linux, vram_gb, cuda}`; Windows boxes claim TRL-native jobs or run
Unsloth jobs under WSL2 (runner detects and reports capability). No Prefect/Dagster in
v1 — the claim-file pattern is already trusted fleet-wide. Stack: Unsloth + TRL + PEFT
(bases and recipes per master plan Pillar 13 — do not re-litigate model choice here).

## Files / areas touched

- `training/jobs/` queue + job-spec schema (dataset hash, recipe, base, adapter out,
  eval bench, requires{})
- `tools/flywheel/runner.py` — claim, capability check, run, push, report
- MLflow docker-compose (self-hosted; HUMAN-GATED hosting box choice)
- `training/recipes/` — SFT/DPO/GRPO config templates (TRL), per master-plan hyperparams
- docs: WSL2 setup note for Windows training hosts

## Definition of done

- [ ] Two machines (one Linux, one Windows) each claim and complete a smoke-size SFT
      job end-to-end (dataset pull → train → adapter push → MLflow run logged)
- [ ] Claim is atomic under concurrent claimers (same guarantee as work.py)
- [ ] Job with `os: linux` is never claimed by a native-Windows runner (test)
- [ ] Every artifact traceable: adapter → job → dataset hash → provenance
- [ ] `graphify update .`; gate green

## Driver notes (Fable, 2026-07-02 — pre-paid pointers + Erik's ratified calls)

- ERIK RATIFIED: MLflow self-hosted via docker on THIS PC — EXTEND the T-H5 compose
  setup in tools/flywheel/ (docker-compose.argilla.yml + .env pattern); add an MLflow
  service (or sibling compose file), do NOT create a second stack or ask again.
- DVC remote = local file:// default (ratified). tools/flywheel/dvc_init.py already has
  the helpers INCLUDING the _dvc_cmd() shutil.which fix (DVC lives at ~/.local/bin/dvc,
  not in mcp-venv) — reuse, don't re-implement.
- Claim queue: mirror work/tools/work.py's atomic claim idiom exactly (stdlib, proven
  fleet-wide). Jobs = files in training/jobs/ with owner field, git-synced. No
  Prefect/Dagster (locked decision).
- requires{os,vram_gb,cuda}: capability check in runner.py; test that os:linux jobs are
  never claimed by a native-Windows runner via monkeypatched platform — no real Windows
  box needed for the unit test; the two-machine smoke DoD is a HUMAN/fleet step, mark it
  [~] with run instructions if you can't execute it.
- Licenses: Unsloth/TRL/PEFT rows (Apache-2.0) in contracts/licenses.md. Recipes are
  config files — they must not execute training in CI (capture-now-train-later stands).
