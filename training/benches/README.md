# Product-task eval benchmark suite (T-H8)

One bench per insertion point that will ship a model (survey §5 item 6, §6). Each bench =
**frozen dataset (content hash)** + **scoring script (through the T-H4 verifier battery)** +
**two baseline rows** (`deterministic` kill-switch, measured; `frontier`, pending T-H7).
Framework + runner live in `tools/flywheel/bench/`; per-bench specs/fixtures + committed
smoke reports live here under `<bench>/`.

| bench | IP | measures | deterministic-baseline (offline) | frontier |
|---|---|---|---|---|
| `regen_fix`   | 18 | %-fixed of a corruption corpus (rebuild + no intent drift) | pct_fixed ≈ 0.40 (2 of 5 mutation classes cleanly fixable) | pending |
| `elicitation` | 12 | slot coverage/precision of conversation→`aicad.req/1` vs gold | coverage ≈ 0.57, schema-valid 1.0 | pending |
| `spec_sketch` | 13 | solver full + dims within tol | in-tol 1.0 (template filler; real residual is upstream NL→spec) | pending |
| `cam`         | 4  | gouge-free + machined-fraction | gouge-free 0.75, hard-fail on any gouge | pending |
| `vlm`         | 21 | claim precision/recall vs known geometry (feeds T-H10) | claim-F1 ≈ 0.91, precision-is-king | pending |

NARRATION (IP-7) is **not** here — it is T-F10's harness (`tools/translator/`). Referenced,
never duplicated.

## Run

```
python -m tools.flywheel.bench.runner --all           # full datasets, writes results/latest.json
python -m tools.flywheel.bench.runner --all --smoke    # gate-size (what gate.sh stage 6 runs)
python -m tools.flywheel.bench.runner regen_fix        # one bench
```

`scripts/gate.sh` stage 6 runs `--smoke --all` only when `CHUDWARE_RUN_BENCH=1` (env-gated SKIP,
OCCT-style) so the gate stays green without the flywheel.

## Design invariants (see `tools/flywheel/bench/DESIGN.md`)

- **Example-level scores** (ROC-AUC where a binary label exists), never only corpus averages.
- **Held-out STRUCTURE splits** (P13: split on structures, not samples) recorded per example.
- **Reproducible from the dataset hash on a second machine/OS** — the content hash is
  EOL-normalized for text members, and generated corpora (regen-fix) are deterministic from
  a committed seed + RNG seed, so no large corpus is committed — only the frozen `spec.json`.
- **RECOGNITION_RESIDUAL discipline**: a bench must show a measured residual (candidate beats
  the deterministic baseline) before any training task for that insertion point starts. Offline
  the frontier candidate is `pending`, so the residual is `pending` until T-H7 logging +
  credentials exist; the deterministic floor is measured now.

## Honesty posture (what is met offline vs gated)

- **Met offline now**: framework, all 5 scorers, the ≥500-doc regen-fix corpus (mutator needs
  no core), all deterministic baselines, cross-OS hash reproducibility, gate smoke. See
  `work/questions/T-H8.md` for the full met-vs-gated split.
- **Gated (documented `pending`, never faked)**: frontier baseline rows (no credentials/network
  offline); elicitation's real ≥50-session gold set (T-H1/T-H5-fed); live-core scoring for
  spec_sketch/CAM/VLM (runs when the C++ core is present — verified reachable: `spec_sketch`'s
  live path builds a sketch into the engine and scores `check_sketch` through the T-H4 battery
  on a live doc).
