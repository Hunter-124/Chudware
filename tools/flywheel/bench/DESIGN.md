# Product-task eval benchmark suite (T-H8)

Source of truth: `docs/review/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §5 item 6 + §6.
One bench per insertion point that will ship a model. Each bench is:

    frozen dataset (content hash / DVC ref)
  + scoring script (scores THROUGH the T-H4 verifier battery — never raw tools)
  + baseline rows: frontier model AND deterministic fallback (the kill-switch comparator)

The benches are the evidence the T-H9 promotion gate consumes: a candidate model
is promoted only on a **shadow-mode win** (verifier-score + accept-rate over N real
jobs) **+ Erik sign-off**. So every bench must emit, per example: a verifier score,
whether it beats the deterministic baseline, and an aggregate a human can read.

## The five v1 benches

| id            | insertion point | dataset                         | score (through battery)                                  | success bar (survey §6) |
|---------------|-----------------|---------------------------------|----------------------------------------------------------|-------------------------|
| `elicitation` | IP-12           | conversation→brief gold set     | slot coverage + precision vs human-audited aicad.req/1   | ≥90% coverage, 100% schema-valid |
| `spec_sketch` | IP-13           | spec→sketch specs               | solver status=full + dims within tol                     | full-solve + in-tol     |
| `regen_fix`   | IP-18           | corruption corpus (≥500 docs)   | %-fixed: rebuild passes + unaffected bodies compare ≈100 | ≥70% auto-fixed, 0 intent-dim drift |
| `cam`         | IP-4            | cam_samples                     | gouge-free + machined-fraction                           | gouge-free, high fraction |
| `vlm`         | IP-21           | synthetic renders + known geom  | claims vs known geometry (feeds T-H10)                   | recall on coarse claims |

NARRATION (IP-7) is **not** here — it is T-F10's harness (`tools/translator/`).
Reference it, never duplicate.

## Shared framework (this directory)

- `schema.py`    — frozen result contract: `BenchResult`, `ExampleScore`. Example-level
                   scores (ROC-AUC-style), never just corpus averages. Held-out
                   STRUCTURE splits recorded per example (P13 rule: hold out structures,
                   not just samples).
- `dataset.py`   — `DatasetSpec` = { id, path, content_hash, dvc_ref, split }. A dataset
                   is frozen by a deterministic content hash over its sorted member
                   files → reproducible from hash on a second machine/OS (DoD). DVC ref
                   is the pointer T-H5 infra resolves; the hash is what CI verifies.
- `baseline.py`  — `Baseline` protocol. Two mandatory rows per bench:
                   `deterministic` (kill-switch comparator, computable offline now) and
                   `frontier` (pluggable model callable; offline/CI records an honest
                   PENDING stub — real rows land when T-H7 logging + credentials exist,
                   same honesty posture as T-F13).
- `metrics.py`   — ROC-AUC, precision/recall, coverage from example scores (stdlib only).
- `runner.py`    — `run_bench(bench, dataset, baselines) -> BenchResult`. Iterates
                   examples, scores each through the battery, aggregates, writes a JSON
                   report under `training/benches/<id>/results/`. `--smoke` runs a small
                   held sample for the gate.
- `benches/`     — one scorer module per bench above.

## RECOGNITION_RESIDUAL discipline

A bench must show a **measured residual** (frontier or human beats the deterministic
baseline by a real margin on the frozen set) before any training task for that
insertion point may start. The runner reports the residual explicitly.

## No new dependencies

Framework is stdlib + the in-repo services/verify/battery. Frontier baselines call the existing
provider layer (T-F9) when credentials exist; offline they stub. No license ledger row.

## Gate hook

`scripts/gate.sh` stage 6 is env-gated SKIP (OCCT pattern): runs `--smoke` benches only
when `CHUDWARE_RUN_BENCH=1` and a Python runtime is present; otherwise records SKIP and the
gate stays green.
