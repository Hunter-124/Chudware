---
id: T-H8
title: Product-task eval benchmark suite (elicitation, spec->sketch, regen-fix, CAM, VLM)
workstream: H
owner: unassigned
status: todo
branch: task/T-H8-eval-benches
day: 13
depends_on: [T-H3, T-H4]
---

## Context

Pillar 16 (`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §5.6, §6). One bench per insertion
point that will ship a model, each = frozen dataset (DVC hash) + scoring script
(T-H4 verifiers) + baseline rows (frontier model AND deterministic fallback — the
kill-switch comparator). v1 benches: (1) elicitation — conversation→aicad.req/1 brief,
slot coverage/precision vs human-audited gold from ≥50 real team sessions; (2)
spec→sketch — solver status=full, dims within tol; (3) regen-fix — corruption corpus:
mutate ≥500 known-good docs into failing rebuilds across feature families, score
%-fixed (rebuild passes + unaffected bodies compare ≈100%); (4) CAM — gouge-free +
machined-fraction on the cam_samples set; (5) VLM render-review — claims vs known
geometry behind synthetic renders (feeds T-H10). NARRATION bench is T-F10's harness —
reference it, never duplicate. Held-out STRUCTURES not just samples (P13 rule).
Optional `gate.sh` stage runs smoke-size benches. RECOGNITION_RESIDUAL go/no-go
discipline: a bench must show a measured residual before any training task starts.

## Files / areas touched

- `tools/flywheel/bench/` — runner + per-bench scorers
- `training/benches/` — frozen dataset specs (DVC refs) + baselines
- corruption-corpus generator (extends the gsl_dataset_gen mutation idiom to doc JSON)
- `scripts/gate.sh` — optional stage 6 model-eval smoke hook (env-gated SKIP like OCCT)

## Definition of done

- [ ] All five benches run end-to-end with both baseline rows populated
- [ ] Bench results reproducible from dataset hash on a second machine/OS
- [ ] Example-level scores (ROC-AUC style), not just corpus averages; held-out
      structure splits documented
- [ ] `graphify update .`; gate green

## Driver notes (Fable, 2026-07-02 — next-wave prep)

- Scoring substrate landed (T-H4): services/verify/battery.py — verify(doc_id,
  checks) + check_metadata() (30 checks, cost/platform/reward_shape tags). Benches
  call IT, never raw tools.
- Requirements gold format landed (T-H3): contracts/requirements.md aicad.req/1;
  VALID_VERIFIERS in engine_oracle/requirements. Post-merge alignment note (see
  FOREMAN 2026-07-02): req_join ↔ contract constants + unit_ranges import — do this
  alignment here if nobody has yet.
- Corruption corpus: mutate known-good docs from corpora/gsl_dataset (document.json
  per sample); mutation classes worth covering: dangling refs (delete a referenced
  feature), param out-of-range, suppressed parent, circular equation, bad selector.
- Narration bench = T-F10's harness (shared, don't duplicate); groundedness module =
  tools/translator/groundedness.py (locked semantics).
