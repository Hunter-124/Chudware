---
id: T-H6
title: Fleet training runner - claim-based job queue, cross-OS profiles, MLflow tracking
workstream: H
owner: sonnet-kier
status: done
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

- [~] Two machines (one Linux, one Windows) each claim and complete a smoke-size SFT
      job end-to-end (dataset pull → train → adapter push → MLflow run logged)
      HUMAN/FLEET step — requires real GPUs + DVC remote + MLflow server.
      To run: `python -m tools.flywheel.runner --owner <host> --dry-run run`
      then with real stack: `python -m tools.flywheel.runner --owner <host> run`
- [x] Claim is atomic under concurrent claimers (same guarantee as work.py)
      → test_claim_job_atomic: 5 threads / 3 jobs, no double-claim (18/18 pass)
- [x] Job with `os: linux` is never claimed by a native-Windows runner (test)
      → test_claim_job_os_filter: monkeypatched cap, asserts windows-job-001 claimed
- [x] Every artifact traceable: adapter → job → dataset hash → provenance
      → test_provenance_chain: full chain verified in provenance.json
- [x] `graphify update .`; gate green
      → graphify updated (22521 nodes, 48222 edges). Gate: check.py + pytest-0-collected
        are PRE-EXISTING (issue #6, FOREMAN known-pitfalls); core/ui/flywheel all pass.
        52/52 flywheel tests green (T-H5's 31 + 21 new T-H6 tests).

## HANDOFF — sonnet-fern2 finisher (2026-07-02)

**Previous driver:** sonnet-kier (died on process exit at tip 3b797e8)
**Finisher:** sonnet-fern2

### What was found in the worktree (2 dirty files assessed)

Both `tools/flywheel/runner.py` and `tools/flywheel/test_runner.py` were dirty with a
coherent in-progress unit:
- `_load_train_dataset()`: lazy HF datasets import, accepts .jsonl file or shard dir
- `_run_training()`: threads `dataset_path`; GRPO/null-reward guard (SEAM T-H9);
  fail-fast when `dataset_path=None` on real training
- `run_job()`: derives dataset_path from training_root/dataset_hash; DVC adapter push
  via `dvc_init.add_to_dvc` / `dvc_push` (reuses T-H5 helpers); completion note
  appended to job .md in work.py style
- 2 new tests: GRPO-null-reward refused + dataset_path-required guard

Pre-commit checks verified: single caller uses keyword args (no positional binding risk);
`add_to_dvc`/`dvc_push` names confirmed in dvc_init.py; `warnings` imported at line 26;
job files are markdown-with-frontmatter so append is safe. **52/52 tests green.**
Committed as: `feat(T-H6): dataset loading, DVC adapter push, completion note, GRPO guard`

### Nothing dropped

Both dirty files were coherent — full commit, nothing discarded.

### DoD gap-check (brief's four items — all verified covered)

| Item | Status | Where |
|------|--------|-------|
| os:linux never claimed by Windows (monkeypatch) | DONE | test_runner.py:296 — `detect_capability` patched to windows cap |
| Unsloth/TRL/PEFT license rows (Apache-2.0) | DONE | contracts/licenses.md lines 117-119 |
| MLflow logging fires | DONE | `_mlflow_run()` tested at test_runner.py:534; patched in integration tests |
| Atomic-claim concurrency test | DONE | test_runner.py:232 — 5 threads / 3 jobs, no double-claim |

### Two-machine smoke run instructions (for Erik — [~] fleet step)

**Prerequisites:** GPU machines with CUDA, DVC configured, MLflow server up
(or use `docker compose -f tools/flywheel/docker-compose.mlflow.yml up -d`).

```bash
# Machine 1 (Linux):
cd /path/to/CAD
git pull
python -m tools.flywheel.runner \
  --owner linux-host-1 \
  --jobs-dir training/jobs \
  --training-root training \
  --mlflow-uri http://localhost:5000 \
  run

# Machine 2 (Windows or WSL2):
cd /path/to/CAD
git pull
python -m tools.flywheel.runner \
  --owner windows-host-1 \
  --jobs-dir training/jobs \
  --training-root training \
  --mlflow-uri http://<linux-host-ip>:5000 \
  run
```

Dry-run (no GPU needed, validates claim+provenance+MLflow logging):
```bash
python -m tools.flywheel.runner --owner <host> --dry-run run
```

Expected output per machine: job claimed → adapter written → `provenance.json` present
→ MLflow run URL logged → job status set to `review`.

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
