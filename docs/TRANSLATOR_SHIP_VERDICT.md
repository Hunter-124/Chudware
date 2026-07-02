# Translator ship verdict (T-F11 v0)

GENERATED companion to the slot-fidelity baseline in `docs/RECOGNITION_RESIDUAL.md`
(`python3 -m tools.translator.eval.harness --append-residual`). Records the Pillar-13 ship
gates' current state and the **kill-switch verdict**. Re-evaluate after provisioning a model.

## Verdict: **HOLD — no ship (pending the downstream measurement)**

A documented no-ship/pending verdict is a valid task completion (T-F11 DoD). No learned
translator is provisioned offline, so the kill-switch WIN axis is unmeasured. The
deterministic template (`gsl_ser.render_caption`) remains the shipping narrator; the learned
translator ships only when it clears the gate below.

## Ship-gate state

| gate | threshold | state (v0) |
|---|---|---|
| numeric fidelity | ≥ 99.5% | **template = 1.00 by construction (ceiling)**; candidate = pending-provision |
| unsupported-claim rate | < 0.1% on held-out **structures** | template = 0.0; candidate = pending |
| slot coverage | ≥ 98% | template = 1.00; candidate = pending |
| round-trip re-extraction | exact | **PASS** (all gold states) |
| scorer self-check | template ~1.0, ROC-AUC 1.0 | **PASS** (validates the metric, not a model) |
| **kill-switch** | beat GSL templates on **downstream task success**, never lose on fidelity | **PENDING** — see measurement below |

**Why fidelity is not the deciding gate (the inversion):** `render_caption` is generated
*from* the ground-truth slots, so it scores ~1.0 fidelity by construction. A learned model
can at best TIE it on fidelity — fidelity/coverage/unsupported are ABSOLUTE safety gates
(and a non-regression bar), NOT a residual to beat. The model earns its place only on the
kill-switch WIN axis, which fidelity cannot show.

## What ships TODAY regardless (safety)

The inference-time **output gate** (`tools/translator/gate.py`, verified behaviorally): any
ungrounded or wrong numeral in a model caption → discard it, emit the deterministic template.
So enabling a local translator can never regress fidelity below the template — the worst
case is "we fell back to templates." This is shippable now, before any training.

## The pending measurement (run this post-provisioning to resolve the kill-switch)

The kill-switch compares **downstream agent task success**, template-narration vs
model-narration — a different metric than slot fidelity, and the only one that justifies the
model. It is unmeasurable offline (needs a served, SFT'd model). Exact spec for Erik/foreman:

1. **Provision + SFT** a candidate (out-of-band, never CI):
   `python -c "from tools.translator.candidates import provision_instructions as p; print(p('all'))"`
   then run `tools/translator/recipes/sft_qlora.yaml` with `AICAD_ALLOW_TRANSLATOR_TRAINING=1`.
2. **Fidelity gate first** (must pass before the kill-switch even runs):
   `python -m tools.translator.candidate_eval` against a served endpoint → require
   fidelity ≥ 0.995, coverage ≥ 0.98, unsupported < 0.001 on held-out **structures**, and
   fidelity ≥ template (non-regression). Winner picked by `pick_winner`.
3. **Downstream-task harness** (the WIN axis): pick a fixed set of N real agent tasks whose
   success is deterministically checkable (e.g. the T-A2 node-runner over a DAG, scored by
   `eval_compare` match_pct / verifier-battery pass, per the T-H8 CAM-bench precedent). Run
   each task **twice** — once with narration = `render_caption` (control), once with
   narration = the gated model (`TranslatorNarrator`) — same seeds, same tasks.
   - **SHIP iff** model-narration task-success > template task-success (strictly) AND
     model fidelity is not below template (never lose on fidelity).
   - **NO-SHIP** otherwise — templates are free; keep the deterministic narrator.
4. Record the two success rates + the decision here and re-run
   `--append-residual`. Until then: **HOLD**.

## Scope note

T-F11 v0 delivered: the output gate (shippable), the deterministic GRPO reward, the
candidate-eval + winner-pick harness (mock-proven), the SFT/GRPO recipe scaffold (CI-guarded),
the serving narrator seam + GBNF grammar, and this verdict. NO training ran (scope fence).
