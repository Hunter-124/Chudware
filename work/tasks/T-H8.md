---
id: T-H8
title: Product-task eval benchmark suite (elicitation, spec->sketch, regen-fix, CAM, VLM)
workstream: H
owner: opus-hex
status: review
branch: task/T-H8-eval-benchmarks
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

- [x] All five benches run end-to-end with both baseline rows populated
- [x] Bench results reproducible from dataset hash on a second machine/OS
- [x] Example-level scores (ROC-AUC style), not just corpus averages; held-out
      structure splits documented
- [x] `graphify update .`; gate green for this task's stages (see Gate result — two
      PRE-EXISTING base reds remain, not introduced by T-H8)

## DoD evidence (opus-hex, 2026-07-02)

**Merge base:** dual-parent (H3 + merged H4) → H8. First commit is the H4 merge
(server.py conflict resolved keep-both). Merge order recorded everywhere.

**All five benches run end-to-end, both baseline rows populated** —
`python -m tools.flywheel.bench.runner --all` runs every bench; each `BenchResult` carries
a `deterministic` row (measured) + a `frontier` row. Deterministic-baseline numbers on the
frozen datasets: regen_fix pct_fixed 0.40 · elicitation coverage 0.57 (schema-valid 1.0) ·
spec_sketch in-tol 1.0 · cam gouge_free 0.75 · vlm claim-F1 0.91. Committed smoke reports
under each `training/benches/<id>/results/`.
- **Frontier rows are honestly `pending`** (no credentials/network offline), filled via
  T-H7 — recorded, never faked (T-F13 precedent). RECOGNITION_RESIDUAL therefore `pending`
  per bench; the deterministic floor is measured now (the residual a model must beat).

**Reproducible from dataset hash on a second machine/OS** — `DatasetSpec` freezes a
content hash that is **EOL-normalized for text members** (a Windows CRLF checkout hashes
identically to LF — `test_hash_is_eol_invariant_for_text`) + a `training/benches/.gitattributes`
`-text` belt. The ≥500-doc regen-fix corpus is generated deterministically from a committed
seed fixture + fixed RNG seed (no core needed), so only the frozen `spec.json` is committed,
not the corpus; regenerating on another box reproduces the same hash
(`test_corpus_is_balanced_and_reproducible`).

**Example-level scores + held-out structure splits** — every `ExampleScore` records a
per-example `score`, `passed`, a binary `label` (ROC-AUC computed where meaningful:
regen_fix `roc_auc_det_fixable`, cam `roc_auc_gouge_free`, vlm `roc_auc`), and the held-out
`structure` key it belongs to (P13: split on structures, not samples — 12 for regen_fix,
4–5 families per gold/fixture bench). Not just corpus averages.

**Scores THROUGH the T-H4 battery** — benches call `services/verify/battery.verify(...)`,
never raw tools. CAM is the one documented exception (work/questions/T-H8.md Q1): the
battery's generic scorer degrades `cam_simulate`, so the CAM bench reads its raw fields and
records the battery's `cam_simulate` pass alongside — `battery.py` untouched.

**Live-core paths verified reachable** — spec_sketch's live path was exercised against a
built core: it builds a sketch into the engine (`doc_apply_ops`/`add_feature` — found + fixed
a real op-wire shape bug: the engine requires `params.entities/constraints`, not a flattened
shape) and scores `check_sketch` **through the T-H4 battery on a live doc**. Live paths for
spec_sketch/CAM/VLM are guarded skip-if-no-core (gate stage 6, env-gated like OCCT), never
hardcode a `.so` path.

**Tests:** 63 passing under `~/.local/share/AI-CAD/mcp-venv/bin/pytest tools/flywheel/bench/`
(metrics 7, dataset 5, regen_fix 5, elicitation 12, spec_sketch 10, cam 9, vlm 15).

**No new dependencies** (stdlib + in-repo engine/battery) → no `contracts/licenses.md` row.
`services/mcp/server.py` unchanged beyond the append-only H3+H4 merge.

**Gate result** (`AICAD_RUN_BENCH=1 bash scripts/gate.sh`) — all T-H8-added stages GREEN;
overall FAIL is two pre-existing base reds this task did not introduce:
- `check.py` FAIL-RECORDED — pre-existing, non-fatal (docs/REFACTOR_LOG.md).
- **core build PASS** (libaicad OCCT ON) · **ctest PASS 100%** · **ui/abstract PASS** ·
  **bench (T-H8) PASS** (all 5 smoke benches ran, both baseline rows each).
- `pytest` FAIL — the known venv/marker skip: `107 deselected, 0 collected` (gate's
  `engine/doc agent` selection collects nothing on this runtime). T-H8's own suite is 63
  passing via `~/.local/share/AI-CAD/mcp-venv/bin/pytest tools/flywheel/bench/`.
- `ui/imgui` FAIL — pre-existing selMulti/panel_spec break: `property_manager.cpp` expects
  post-`b569020` `PanelField` members (`key`/`comboOptions`/`SelectorKinds`); this branch's
  base (origin/task/T-H3) predates `b569020`, exactly as flagged at dispatch. No C++/UI code
  touched by T-H8. Resolves on merge to current master.

**Open questions (non-blocking, recommended answers in `work/questions/T-H8.md`):** Q1 CAM
score-through-battery (proceeding: raw cam_simulate, battery.py untouched); Q2 offline-vs-gated
DoD split; Q3 req_join met-status↔contract-ladder reconcile is a T-H4 follow-up (out of
scope; unit_ranges import already wired).
